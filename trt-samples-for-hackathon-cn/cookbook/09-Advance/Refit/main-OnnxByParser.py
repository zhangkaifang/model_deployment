#
# Copyright (c) 2021-2023, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
from datetime import datetime as dt
from glob import glob

import cv2
import numpy as np
import onnx
import onnx_graphsurgeon as gs
import tensorrt as trt
import torch as t
import torch.nn.functional as F
from cuda import cudart
from torch.autograd import Variable

np.random.seed(31193)
t.manual_seed(97)
t.cuda.manual_seed_all(97)
t.backends.cudnn.deterministic = True
nTrainBatchSize = 128
nInferBatchSize = 1
nHeight = 28
nWidth = 28
onnxFile0 = "./model0.onnx"
onnxFile1 = "./model1.onnx"
trtFile = "./model.plan"
dataPath = os.path.dirname(os.path.realpath(__file__)) + "/../../00-MNISTData/"
trainFileList = sorted(glob(dataPath + "train/*.jpg"))
testFileList = sorted(glob(dataPath + "test/*.jpg"))
inferenceImage = dataPath + "8.png"

os.system("rm -rf ./*.onnx ./*.plan")
np.set_printoptions(precision=3, linewidth=200, suppress=True)
cudart.cudaDeviceSynchronize()

# Create network and train model in pyTorch ------------------------------------
class Net(t.nn.Module):

    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = t.nn.Conv2d(1, 32, (5, 5), padding=(2, 2), bias=True)
        self.conv2 = t.nn.Conv2d(32, 64, (5, 5), padding=(2, 2), bias=True)
        self.fc1 = t.nn.Linear(64 * 7 * 7, 1024, bias=True)
        self.fc2 = t.nn.Linear(1024, 10, bias=True)

    def forward(self, x):
        x = F.max_pool2d(F.relu(self.conv1(x)), (2, 2))
        x = F.max_pool2d(F.relu(self.conv2(x)), (2, 2))
        x = x.reshape(-1, 64 * 7 * 7)
        x = F.relu(self.fc1(x))
        y = self.fc2(x)
        z = F.softmax(y, dim=1)
        z = t.argmax(z, dim=1)
        return y, z

class MyData(t.utils.data.Dataset):

    def __init__(self, isTrain=True):
        if isTrain:
            self.data = trainFileList
        else:
            self.data = testFileList

    def __getitem__(self, index):
        imageName = self.data[index]
        data = cv2.imread(imageName, cv2.IMREAD_GRAYSCALE)
        label = np.zeros(10, dtype=np.float32)
        index = int(imageName[-7])
        label[index] = 1
        return t.from_numpy(data.reshape(1, nHeight, nWidth).astype(np.float32)), t.from_numpy(label)

    def __len__(self):
        return len(self.data)

model = Net().cuda()

# Export an untrained model as ONNX file ---------------------------------------
t.onnx.export( \
    model,
    t.randn(1, 1, nHeight, nWidth, device="cuda"),
    onnxFile0,
    input_names=["x"],
    output_names=["y", "z"],
    do_constant_folding=True,
    verbose=True,
    keep_initializers_as_inputs=True,
    opset_version=12,
    dynamic_axes={"x": {0: "nBatchSize"}, "z": {0: "nBatchSize"}})

ceLoss = t.nn.CrossEntropyLoss()
opt = t.optim.Adam(model.parameters(), lr=0.001)
trainDataset = MyData(True)
testDataset = MyData(False)
trainLoader = t.utils.data.DataLoader(dataset=trainDataset, batch_size=nTrainBatchSize, shuffle=True)
testLoader = t.utils.data.DataLoader(dataset=testDataset, batch_size=nTrainBatchSize, shuffle=True)

for epoch in range(10):
    for xTrain, yTrain in trainLoader:
        xTrain = Variable(xTrain).cuda()
        yTrain = Variable(yTrain).cuda()
        opt.zero_grad()
        y_, z = model(xTrain)
        loss = ceLoss(y_, yTrain)
        loss.backward()
        opt.step()

    with t.no_grad():
        acc = 0
        n = 0
        for xTest, yTest in testLoader:
            xTest = Variable(xTest).cuda()
            yTest = Variable(yTest).cuda()
            y_, z = model(xTest)
            acc += t.sum(z == t.matmul(yTest, t.Tensor([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]).to("cuda:0"))).cpu().numpy()
            n += xTest.shape[0]
        print("%s, epoch %2d, loss = %f, test acc = %f" % (dt.now(), epoch + 1, loss.data, acc / n))

print("Succeeded building model in pyTorch!")

# Export model as ONNX file ----------------------------------------------------
t.onnx.export( \
    model,
    t.randn(1, 1, nHeight, nWidth, device="cuda"),
    onnxFile1,
    input_names=["x"],
    output_names=["y", "z"],
    do_constant_folding=True,
    verbose=True,
    keep_initializers_as_inputs=True,
    opset_version=12,
    dynamic_axes={"x": {0: "nBatchSize"}, "z": {0: "nBatchSize"}})
print("Succeeded converting model into ONNX!")

# # Dynamic Shape mode + Refit is supported since TensorRT-8.5, or we must use Static Shape model
a, b, c = [int(i) for i in trt.__version__.split(".")]
if a < 8 or a == 8 and b < 5:
    for file in [onnxFile0, onnxFile1]:
        graph = gs.import_onnx(onnx.load(file))
        graph.inputs[0].shape = [nInferBatchSize, 1, 28, 28]
        graph.cleanup()
        onnx.save(gs.export_onnx(graph), file)
    print("Succeeded converting model into static shape!")

# Parse network, rebuild network and do inference in TensorRT ------------------
def run():
    logger = trt.Logger(trt.Logger.WARNING)
    if os.path.isfile(trtFile):
        with open(trtFile, "rb") as f:
            engine = trt.Runtime(logger).deserialize_cuda_engine(f.read())
        if engine == None:
            print("Failed loading engine!")
            exit()
        print("Succeeded loading engine!")
        onnxFile = onnxFile1  # we already have model.plan, so load model1.onnx to refit engine
    else:
        onnxFile = onnxFile0  # we do not have model.plan, so load model0.onnx to build engine

    builder = trt.Builder(logger)
    network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
    config = builder.create_builder_config()
    config.set_flag(trt.BuilderFlag.REFIT)
    parser = trt.OnnxParser(network, logger)
    if not os.path.exists(onnxFile):
        print("Failed finding ONNX file!")
        exit()
    print("Succeeded finding ONNX file!")
    with open(onnxFile, "rb") as model:
        if not parser.parse(model.read()):
            print("Failed parsing .onnx file!")
            for error in range(parser.num_errors):
                print(parser.get_error(error))
            exit()
        print("Succeeded parsing .onnx file!")

    if os.path.isfile(trtFile):  # Refit
        refitter = trt.Refitter(engine, logger)
        layerNameList, weightRoleList = refitter.get_all()  # get name and role of the refitable weights
        #layerNameList = refitter.get_all_weights()  # only get name of the refitable weights
        #tensorDynamicRangeList = refitter.get_tensors_with_dynamic_range()  # get dynamic range of all tensors in INT8 mode
        
        print("[Name,\tRole]")
        for name, role in zip(layerNameList, weightRoleList):
            print("[%s,%s]" % (name, role))

        for i in range(network.num_layers):
            layer = network.get_layer(i)
            if layer.name in layerNameList:

                if layer.type == trt.LayerType.CONVOLUTION:
                    layer.__class__ = trt.IConvolutionLayer
                    refitter.set_weights(layer.name, trt.WeightsRole.KERNEL, layer.kernel)
                    refitter.set_weights(layer.name, trt.WeightsRole.BIAS, layer.bias)
                    #oldRange = refitter.get_dynamic_range(layer.get_output(0).name)  # get tensor's old dynamic range
                    #refitter.set_dynamic_range(layer.get_output(0).name, layer.get_output(0).dynamic_range)  # set tensor's dynamic range in INT8 mode
                    #refitter.set_dynamic_range(layer.get_output(0).name, oldRange)  # or use old dynamic range as new range

                elif layer.type == trt.LayerType.DECONVOLUTION:
                    layer.__class__ = trt.IConvolutionLayer
                    refitter.set_weights(layer.name, trt.WeightsRole.KERNEL, layer.kernel)
                    refitter.set_weights(layer.name, trt.WeightsRole.BIAS, layer.bias)
                    #refitter.set_dynamic_range(layer.get_output(0).name, layer.get_output(0).dynamic_range)
                    #refitter.set_dynamic_range(layer.get_output(0).name, oldRange)

                elif layer.type == trt.LayerType.FULLY_CONNECTED:
                    layer.__class__ = trt.IFullyConnectedLayer
                    refitter.set_weights(layer.name, trt.WeightsRole.KERNEL, layer.kernel)
                    #refitter.set_dynamic_range(layer.get_output(0).name, layer.get_output(0).dynamic_range)
                    #refitter.set_dynamic_range(layer.get_output(0).name, oldRange)

                elif layer.type == trt.LayerType.CONSTANT:
                    layer.__class__ = trt.IConstantLayer
                    refitter.set_weights(layer.name, trt.WeightsRole.CONSTANT, layer.weights)
                    #refitter.set_dynamic_range(layer.get_output(0).name, layer.get_output(0).dynamic_range)
                    #refitter.set_dynamic_range(layer.get_output(0).name, oldRange)

                elif layer.type == trt.LayerType.SCALE:
                    layer.__class__ = trt.IScaleLayer
                    refitter.set_weights(layer.name, trt.WeightsRole.SHIFT, layer.shift)
                    refitter.set_weights(layer.name, trt.WeightsRole.SCALE, layer.scale)
                    #refitter.set_dynamic_range(layer.get_output(0).name, layer.get_output(0).dynamic_range)
                    #refitter.set_dynamic_range(layer.get_output(0).name, oldRange)

                elif True:  # More branch might be needed
                    pass

        refitter.refit_cuda_engine()

    else:  # build engine
        """
        # print the network, for debug
        for i in range(network.num_layers):
            layer = network.get_layer(i)        
            print(i, "%s,in=%d,out=%d,%s" % (str(layer.type)[10:], layer.num_inputs, layer.num_outputs, layer.name))
            for j in range(layer.num_inputs):
                tensor = layer.get_input(j)
                if tensor == None:
                    print("\tInput  %2d:" % j, "None")
                else:
                    print("\tInput  %2d:%s,%s,%s" % (j, tensor.shape, str(tensor.dtype)[9:], tensor.name))
            for j in range(layer.num_outputs):
                tensor = layer.get_output(j)
                if tensor == None:
                    print("\tOutput %2d:" % j, "None")
                else:
                    print("\tOutput %2d:%s,%s,%s" % (j, tensor.shape, str(tensor.dtype)[9:], tensor.name))
        """

        profile = builder.create_optimization_profile()
        inputTensor = network.get_input(0)
        profile.set_shape(inputTensor.name, [1, 1, nHeight, nWidth], [4, 1, nHeight, nWidth], [8, 1, nHeight, nWidth])
        config.add_optimization_profile(profile)

        engineString = builder.build_serialized_network(network, config)
        with open(trtFile, "wb") as f:
            f.write(engineString)
        engine = trt.Runtime(logger).deserialize_cuda_engine(engineString)

    nIO = engine.num_io_tensors
    lTensorName = [engine.get_tensor_name(i) for i in range(nIO)]
    nInput = [engine.get_tensor_mode(lTensorName[i]) for i in range(nIO)].count(trt.TensorIOMode.INPUT)

    context = engine.create_execution_context()
    context.set_input_shape(lTensorName[0], [1, 1, nHeight, nWidth])
    for i in range(nIO):
        print("[%2d]%s->" % (i, "Input " if i < nInput else "Output"), engine.get_tensor_dtype(lTensorName[i]), engine.get_tensor_shape(lTensorName[i]), context.get_tensor_shape(lTensorName[i]), lTensorName[i])

    data = cv2.imread(inferenceImage, cv2.IMREAD_GRAYSCALE).astype(np.float32)
    data = np.tile(data, [nInferBatchSize, 1, 1, 1])
    bufferH = []
    bufferH.append(np.ascontiguousarray(data))
    for i in range(nInput, nIO):
        bufferH.append(np.empty(context.get_tensor_shape(lTensorName[i]), dtype=trt.nptype(engine.get_tensor_dtype(lTensorName[i]))))
    bufferD = []
    for i in range(nIO):
        bufferD.append(cudart.cudaMalloc(bufferH[i].nbytes)[1])

    for i in range(nInput):
        cudart.cudaMemcpy(bufferD[i], bufferH[i].ctypes.data, bufferH[i].nbytes, cudart.cudaMemcpyKind.cudaMemcpyHostToDevice)

    for i in range(nIO):
        context.set_tensor_address(lTensorName[i], int(bufferD[i]))

    context.execute_async_v3(0)

    for i in range(nInput, nIO):
        cudart.cudaMemcpy(bufferH[i].ctypes.data, bufferD[i], bufferH[i].nbytes, cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost)

    for i in range(nIO):
        print(lTensorName[i])
        print(bufferH[i])

    for b in bufferD:
        cudart.cudaFree(b)
    print("Succeeded running model in TensorRT!")

run()  # build engine
run()  # Refit engine