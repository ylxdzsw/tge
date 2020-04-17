import time
import numpy as np
import tensorflow as tf
import os

from models import GAT
from gat_utils import process
from data_process.dataset import GraphDataset, WhiteSpaceTokenizer,NewWhiteSpaceTokenizer
from data_process.example import load_M10, load_cora, load_dblp
from data_process.meta_network import MetaNetwork, N_TYPE_NODE, N_TYPE_LABEL, IdIndexer
import google.protobuf.text_format as pbtf
from tensorflow.core.framework import graph_pb2
import copy
import sys
import json
import pickle as pkl
sys.path.append('../')
import tge
prefix=sys.argv[1]
from utils import adapt_batchsize

config_dict =dict()
if os.path.exists("config.txt"):
    with open("config.txt", "r") as f:
        config_dict = json.load(f)
devices=config_dict.get("devices", [
    "/job:tge/replica:0/task:0/device:GPU:0",
    "/job:tge/replica:0/task:0/device:GPU:1",
    "/job:tge/replica:0/task:1/device:GPU:0",
    "/job:tge/replica:0/task:1/device:GPU:1"
])
device_mems=config_dict.get("device_mems", [16*10e9,16*10e9,16*10e9,16*10e9])

sink=["GradientDescent"]
class Environment(object):
    def __init__(self,gdef_path,null_gdef_path,devices,folder):

        self.gdef = graph_pb2.GraphDef()
        with open(gdef_path,"r")as f:
            txt = f.read()
        pbtf.Parse(txt,self.gdef)

        self.null_gdef = graph_pb2.GraphDef()
        with open(null_gdef_path,"r")as f:
            txt = f.read()
        pbtf.Parse(txt,self.null_gdef)

        self.folder = folder
        self.strategy_reward_dict=dict()
        self.name_cost_dict = self.get_name_cost_dict()
        self.devices =devices
        self._tge = tge.TGE(self.gdef, devices)
        self.global_batch_size = 288*6 if "graph7" in null_gdef_path else 48*6

        with open("nccl_model.pkl","rb") as f:
            self.nccl_model=pkl.load(f)

    def get_reward(self,strategy,index_id_dict,trace=""):
        bandwidth = config_dict.get("bandwidth",None)
        if bandwidth==None:
            intra = "5000"
            inter = "1250"
        else:
            intra = bandwidth[0]
            inter = bandwidth[1]
        time_mem_tuple = tge.TGE(copy.deepcopy(self.gdef), self.devices,sink).set_nccl_model(self.nccl_model).use_collective().custom({index_id_dict[index]:strategy_int for index,strategy_int in enumerate(strategy)}).set_bandwidth(intra,inter).evaluate(self.name_cost_dict,trace)
        time = time_mem_tuple[0]
        mem_list = time_mem_tuple[1]
        time = float(time) / (10 ** 3)
        if any(np.array(mem_list) > np.array(device_mems)):
            time = time * 10
            print("oom")
        self.strategy_reward_dict[str(strategy)]=time
        return np.float32(time)

    def get_null_reward(self,strategy,index_id_dict,trace=""):
        name_list = [nodedef.name for nodedef in self.null_gdef.node]
        strategy = {index_id_dict[index]: strategy_int for index, strategy_int in enumerate(strategy)}
        strategy = {name: strategy.get(name, list(strategy.values())[0]) for name in name_list}
        bandwidth = config_dict.get("bandwidth",None)
        if bandwidth==None:
            intra = "5000"
            inter = "1250"
        else:
            intra = bandwidth[0]
            inter = bandwidth[1]
        time_mem_tuple = tge.TGE(copy.deepcopy(self.null_gdef), self.devices,sink).fill_batchsize(self.global_batch_size).set_nccl_model(self.nccl_model).use_collective().custom(strategy).set_bandwidth(intra,inter).evaluate(self.name_cost_dict,trace)
        time = time_mem_tuple[0]
        mem_list = time_mem_tuple[1]
        print(mem_list)
        time = float(time) / (10 ** 3)

        if any(np.array(mem_list) > np.array(device_mems)):
            time = time * 10
            print("oom")
        self.strategy_reward_dict[str(strategy)]=time
        return np.float32(time)

    def get_name_cost_dict(self):
        with open(self.folder+"/new_cost.pkl", "rb") as f:
            name_cost_dict = pkl.load(f)
            #name_cost_dict = adapt_batchsize(name_cost_dict,48,100,20)

        return name_cost_dict

env = Environment(prefix+"/graph.pbtxt",prefix+"/null_graph.pbtxt",devices,prefix)
dataset = load_cora(prefix,NewWhiteSpaceTokenizer())
index_id_dict = dataset.network.get_indexer(N_TYPE_NODE).index_id_dict
feature_matrix, feature_masks = dataset.feature_matrix(bag_of_words=False, sparse=False)
nb_nodes = feature_matrix.shape[0]

with open("test_config.json","r") as f:
    tmp = json.load(f)
    strategies = tmp["strategies"]
for _strategy in strategies:
    strategy = list()
    for i in range(nb_nodes):
        strategy.append(_strategy)
    arr_strategy = np.array(strategy)
    print("strategy:",_strategy)
    #print(env.get_reward(arr_strategy,index_id_dict,prefix+"/"+str(_strategy)+".json"))
    print(env.get_null_reward(arr_strategy,index_id_dict,prefix+"/"+str(_strategy)+"_null.json"))

'''
name_cost_dict = env.get_name_cost_dict()
cost = list(name_cost_dict.values())
cost.sort()
for name in name_cost_dict.keys():
    if "Back" in name:
        print(name,name_cost_dict[name])
    if name_cost_dict[name]>cost[-100]:
        print(name,name_cost_dict[name])
'''     
