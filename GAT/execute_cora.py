import time
import numpy as np
import tensorflow as tf

from models import GAT
from models import SpGAT
from utils import process
from data_process.dataset import GraphDataset, WhiteSpaceTokenizer,NewWhiteSpaceTokenizer
from data_process.example import load_M10, load_cora, load_dblp
from data_process.meta_network import MetaNetwork, N_TYPE_NODE, N_TYPE_LABEL, IdIndexer
import google.protobuf.text_format as pbtf
from tensorflow.core.framework import graph_pb2
from sklearn.preprocessing import StandardScaler
import copy
import sys
import os
import scipy.sparse as sp

import pickle
sys.path.append('../')
import tge
import json
import pickle as pkl

checkpt_file = 'pre_trained/cora/mod_cora.ckpt'
_dataset = 'cora'


config_dict =dict()
if os.path.exists("config.txt"):
    with open("config.txt", "r") as f:
        config_dict = json.load(f)

# training params
batch_size = 1
nb_epochs = 100000
patience = 100
lr = config_dict.get("learning_rate",0.01)  # learning rate
l2_coef = 0.0005  # weight decay
hid_units = [256,256] # numbers of hidden units per each attention head in each layer
n_heads = [4,4, 6] # additional entry for the output layer
residual = False
nonlinearity = tf.nn.elu
model = SpGAT
transformer = True
print('Dataset: ' + _dataset)
print('----- Opt. hyperparams -----')
print('lr: ' + str(lr))
print('l2_coef: ' + str(l2_coef))
print('----- Archi. hyperparams -----')
print('nb. layers: ' + str(len(hid_units)))
print('nb. units per layer: ' + str(hid_units))
print('nb. attention heads: ' + str(n_heads))
print('residual: ' + str(residual))
print('nonlinearity: ' + str(nonlinearity))
print('model: ' + str(model))
feature_folders = ["data/graph1","data/graph2","data/graph3","data/graph4","data/graph5","data/graph6"]
sample_times = 3
devices = (
    "/job:tge/replica:0/task:0/device:GPU:0",
    "/job:tge/replica:0/task:0/device:GPU:1",
    "/job:tge/replica:0/task:1/device:GPU:0",
    "/job:tge/replica:0/task:1/device:GPU:1"
)
show_interval = 3


def rel_multihead_attn(w, r,d_model,mems,
                       n_head, d_head, nb_nodes,scope='rel_attn'):
    scale = 1 / (d_head ** 0.5)
    cat = w
    if False:
        w = tf.expand_dims(w, axis=0)
        seq_fts = tf.layers.conv1d(w, d_model, 1, use_bias=False)

        # simplest self-attention possible
        f_1 = tf.layers.conv1d(seq_fts, 1, 1)
        f_2 = tf.layers.conv1d(seq_fts, 1, 1)

        f_1 = tf.reshape(f_1, (nb_nodes, 1))
        f_2 = tf.reshape(f_2, (nb_nodes, 1))

        f_1 = r * f_1

        f_2 = r * tf.transpose(f_2, [1, 0])


        logits = tf.sparse_add(f_1, f_2)
        lrelu = tf.SparseTensor(indices=logits.indices,
                                values=tf.nn.leaky_relu(logits.values),
                                dense_shape=logits.dense_shape)
        coefs = tf.sparse_softmax(lrelu)

        # As tf.sparse_tensor_dense_matmul expects its arguments to have rank-2,
        # here we make an assumption that our input is of batch size 1, and reshape appropriately.
        # The method will fail in all other cases!
        coefs = tf.sparse_reshape(coefs, [nb_nodes, nb_nodes])
        seq_fts = tf.reshape(seq_fts,(nb_nodes,seq_fts.shape[-1]))
        vals = tf.sparse_tensor_dense_matmul(coefs, seq_fts)
        vals = tf.reshape(vals,(nb_nodes,seq_fts.shape[-1]))
        w_heads = tf.layers.dense(cat, 3 * n_head * d_head, use_bias=False)
        r_head_k = tf.layers.dense(vals, n_head * d_head, use_bias=False)



        w_head_q, w_head_k, w_head_v = tf.split(w_heads, 3, -1)


        rw_head_q = w_head_q

       # AC = tf.einsum('ib,jb->ij', rw_head_q, w_head_k)
        print(rw_head_q.shape)
        print(tf.transpose(w_head_k).shape)

        AC =tf.matmul(rw_head_q,tf.transpose(w_head_k))
        BD =tf.matmul(rw_head_q,tf.transpose(r_head_k))
        print(AC.shape)
        print(BD.shape)
        attn_score = (AC+BD) * scale
        attn_prob = tf.nn.softmax(attn_score, 1)

        attn_vec = tf.matmul(attn_prob, w_head_v)
        # size_t = tf.shape(attn_vec)
        # attn_vec = tf.reshape(attn_vec, [size_t[0], size_t[1], n_head * d_head])

        attn_out = tf.layers.dense(attn_vec, d_model, use_bias=False, activation=tf.nn.leaky_relu)
        output = tf.contrib.layers.layer_norm(attn_out + cat, begin_norm_axis=-1)
    else:
        w = tf.expand_dims(w, axis=0)
        qlen = tf.shape(w)[0]
        bsz = tf.shape(w)[1]

        cat = tf.concat([mems, w],
                        0) if mems is not None and mems.shape.ndims > 1 else w
        cat=w
        w_heads = tf.layers.dense(cat, 3 * n_head * d_head, use_bias=False)

        w_head_q, w_head_k, w_head_v = tf.split(w_heads, 3, -1)
        w_head_q = w_head_q[-qlen:]

        klen = tf.shape(w_head_k)[0]

        w_head_q = tf.reshape(w_head_q, [qlen, bsz, n_head, d_head])
        w_head_k = tf.reshape(w_head_k, [klen, bsz, n_head, d_head])
        w_head_v = tf.reshape(w_head_v, [klen, bsz, n_head, d_head])

        rw_head_q = w_head_q

        AC = tf.einsum('ibnd,jbnd->ijbn', rw_head_q, w_head_k)


        attn_score = (AC) * scale

       # w_heads = tf.layers.dense(cat, 3 * n_head * d_head, use_bias=False)
       # w_head_q, w_head_k, w_head_v = tf.split(w_heads, 3, -1)
       # rw_head_q = w_head_q
       # AC =tf.matmul(rw_head_q,tf.transpose(w_head_k))
       # attn_score = (AC) * scale



        attn_prob = tf.nn.softmax(attn_score, 1)

        #attn_vec = tf.matmul(attn_prob,w_head_v)
        attn_vec = tf.einsum('ijbn,jbnd->ibnd', attn_prob, w_head_v)
        size_t = tf.shape(attn_vec)
        attn_vec = tf.reshape(attn_vec, [size_t[0], size_t[1], n_head * d_head])

        attn_out = tf.layers.dense(attn_vec, d_model, use_bias=False)
        output = tf.contrib.layers.layer_norm(attn_out + cat, begin_norm_axis=-1)
    return output[0]

class strategy_pool(object):
    def __init__(self,folder_path,node_num,index_id_dict,env):
        self.folder_path = folder_path
        self.node_num = node_num
        self.index_id_dict = index_id_dict
        self.env = env
        if os.path.exists(self.folder_path+"/pool.pkl"):
            with open(self.folder_path+"/pool.pkl","rb") as f:
                self.strategies= pkl.load(f)
            self.save_strategy_pool()
        else:
            self.strategies = list()

        self.rewards = [item["reward"] for item in self.strategies] if len(self.strategies) else [0]

        # data parallel
        #device_choice = np.zeros(shape=(self.node_num, len(devices)), dtype=np.int32)
        device_choice = np.array([np.arange(len(devices)) for i in range(self.node_num)])
        ps_or_reduce = np.ones(shape=(self.node_num, ), dtype=np.int32)
        reward = self.env.get_reward2(device_choice, ps_or_reduce, self.index_id_dict)
        self.insert(reward, device_choice, ps_or_reduce)

        #single gpu
        device_choice = np.negative(np.ones(shape=(self.node_num, len(devices)), dtype=np.int32))
        for item in device_choice:
            item[0] =0
        ps_or_reduce = np.ones(shape=(self.node_num, ), dtype=np.int32)
        reward = self.env.get_reward2(device_choice, ps_or_reduce, self.index_id_dict)
        self.insert(reward, device_choice, ps_or_reduce)

        self.rewards = [item["reward"] for item in self.strategies]

    def get_stratey_list(self,device_choice,ps_or_reduce):
        new_device_array = np.zeros(shape=device_choice.shape,dtype=np.int32)
        for i in range(device_choice.shape[0]):
            for j in range(device_choice.shape[1]):
                if device_choice[i,j]!=-1 and device_choice[i,j]!=len(devices):
                    new_device_array[i,device_choice[i,j]]+=1
        ps_or_reduce = np.reshape(ps_or_reduce, (ps_or_reduce.shape[0], 1))
        new_device_array = np.concatenate((ps_or_reduce,new_device_array),axis=1)
        return new_device_array.tolist()

    def save_strategy_pool(self):
        with open(self.folder_path + "/pool.pkl", "wb") as f:
            pkl.dump(self.strategies,f)
        with open(self.folder_path + "/pool.log", "w") as f:
            f.write(str([item["reward"] for item in self.strategies]))
            for i in range(4):
                f.write("\nstrategy"+str(i)+":\n")
                f.write(str(self.strategies[np.random.randint(len(self.strategies))]["strategy_list"]))


    def insert(self,reward,device_choice,ps_or_reduce):
        strategy_list = self.get_stratey_list(device_choice, ps_or_reduce)
        if len(self.strategies)<20:
            for strategy in self.strategies:
                exist_device_choice = (strategy["device_choice"])
                diff_list = [0 if all(device_choice[i]==exist_device_choice[i]) else 1 for i in range(len(device_choice))]
                if sum(diff_list)/len(diff_list)<0.01:
                    if reward>strategy["reward"]:
                        self.strategies.append({"strategy_list":strategy_list,"reward":reward,"device_choice":device_choice,"ps_or_reduce":ps_or_reduce})
                        self.strategies.remove(strategy)
                        self.save_strategy_pool()
                        self.rewards = [item["reward"] for item in self.strategies]
                    return
            self.strategies.append({"strategy_list": strategy_list, "reward": reward, "device_choice": device_choice,
                                    "ps_or_reduce": ps_or_reduce})
            self.save_strategy_pool()
            self.rewards.append(reward)
        elif len(self.strategies)<200 and reward>np.mean(self.rewards):
            for strategy in self.strategies:
                exist_device_choice = (strategy["device_choice"])
                diff_list = [0 if all(device_choice[i]==exist_device_choice[i]) else 1 for i in range(len(device_choice))]
                if sum(diff_list)/len(diff_list)<0.01:
                    if reward>strategy["reward"]:
                        self.strategies.append({"strategy_list":strategy_list,"reward":reward,"device_choice":device_choice,"ps_or_reduce":ps_or_reduce})
                        self.strategies.remove(strategy)
                        self.save_strategy_pool()
                        self.rewards = [item["reward"] for item in self.strategies]
                    return
            self.strategies.append({"strategy_list": strategy_list, "reward": reward, "device_choice": device_choice,
                                    "ps_or_reduce": ps_or_reduce})
            self.save_strategy_pool()
            self.rewards.append(reward)
        elif len(self.strategies)>=200 and reward>np.mean(self.rewards):
            for strategy in self.strategies:
                exist_device_choice = (strategy["device_choice"])
                diff_list = [0 if all(device_choice[i]==exist_device_choice[i]) else 1 for i in range(len(device_choice))]
                if sum(diff_list)/len(diff_list)<0.01:
                    if reward>strategy["reward"]:
                        self.strategies.append({"strategy_list":strategy_list,"reward":reward,"device_choice":device_choice,"ps_or_reduce":ps_or_reduce})
                        self.strategies.remove(strategy)
                        self.save_strategy_pool()
                        self.rewards = [item["reward"] for item in self.strategies]
                    return
            index = self.rewards.index(min(self.rewards))
            self.strategies.remove(self.strategies[index])
            self.strategies.append({"strategy_list": strategy_list, "reward": reward, "device_choice": device_choice,
                                    "ps_or_reduce": ps_or_reduce})
            self.save_strategy_pool()
            self.rewards = [item["reward"] for item in self.strategies]

    def choose_strategy(self):
        index = np.random.randint(0,len(self.strategies))
        return self.strategies[index]

class Environment(object):
    def __init__(self,gdef_path,devices,folder_path):

        self.gdef = graph_pb2.GraphDef()
        with open(gdef_path,"r")as f:
            txt = f.read()
        pbtf.Parse(txt,self.gdef)
        self.folder_path = folder_path
        self.random_strategy=list()
        self.best_time = sys.maxsize
        self.best_strategy = dict()
        self.strategy_time_dict=dict()

        if os.path.exists(folder_path+"/best_time.log"):
            with open(folder_path+"/best_time.log", "r") as f:
                tmp = json.load(f)
                self.best_time = tmp["time"]
                self.best_strategy = tmp["strategy"]
                self.strategy_time_dict[str(self.best_strategy)] = self.best_time


        self.devices =devices
        self.name_cost_dict = self.get_name_cost_dict()
        self._tge = tge.TGE(self.gdef, devices)
    def get_stratey_array(self,device_choice,ps_or_reduce):

        new_device_array = np.zeros(shape=device_choice.shape,dtype=np.int32)
        for i in range(device_choice.shape[0]):
            for j in range(device_choice.shape[1]):
                if device_choice[i,j]!=-1 and device_choice[i,j]!=len(self.devices):
                    new_device_array[i,device_choice[i,j]]+=1
        ps_or_reduce = np.reshape(ps_or_reduce, (ps_or_reduce.shape[0], 1))
        new_device_array = np.concatenate((ps_or_reduce,new_device_array),axis=1)
        return new_device_array
    def get_reward2(self,device_choice,ps_or_reduce,index_id_dict):

        new_device_array = np.zeros(shape=device_choice.shape,dtype=np.int32)
        for i in range(device_choice.shape[0]):
            for j in range(device_choice.shape[1]):
                if device_choice[i,j]!=-1 and device_choice[i,j]!=len(self.devices):
                    new_device_array[i,device_choice[i,j]]+=1
        ps_or_reduce = np.reshape(ps_or_reduce, (ps_or_reduce.shape[0], 1))
        new_device_array = np.concatenate((ps_or_reduce,new_device_array),axis=1)
        strategy = {index_id_dict[index]:new_device_array[index].tolist() for index in range(new_device_array.shape[0])}
        bandwidth = config_dict.get("bandwidth",None)
        if bandwidth==None:
            intra = "5000"
            inter = "1250"
        else:
            intra = bandwidth[0]
            inter = bandwidth[1]
        time = tge.TGE(copy.deepcopy(self.gdef), self.devices).custom(strategy).set_bandwidth(intra,inter).evaluate(self.name_cost_dict)
        time = float(time)/(10**6)
        #reward = np.sum(strategy*strategy)
        if time<self.best_time:
            self.best_time = time
            self.best_strategy = strategy
            with open(self.folder_path+"/best_time.log", "w") as f:
                tmp = dict()
                tmp["time"] = time
                tmp["strategy"] = self.best_strategy
                tmp["cost"] = self.name_cost_dict
                json.dump(tmp, f)

        return -np.float32(np.sqrt(time))

    def get_name_cost_dict(self):
        name_cost_dict = dict()
        with open(self.folder_path+"/docs.txt", "r") as f:
            for line in f.readlines():
                line = line.strip()
                items = line.split(" ")
                name = items[0]
                cost = list(np.array(items[-len(devices):]))
                name_cost_dict[name] = cost
        return name_cost_dict



class feature_item(object):
    def __init__(self,folder_path):
        self.dataset = load_cora(folder_path,NewWhiteSpaceTokenizer())
        adj = self.dataset.adj_matrix(sparse=True)
        feature_matrix, feature_masks = self.dataset.feature_matrix(bag_of_words=False, sparse=False)

        feature_matrix = StandardScaler().fit_transform(feature_matrix)

        labels, label_masks = self.dataset.label_list_or_matrix(one_hot=False)

        train_node_indices, test_node_indices, train_masks, test_masks = self.dataset.split_train_and_test(training_rate=0.8)

        self.nb_nodes = feature_matrix.shape[0]
        self.ft_size = feature_matrix.shape[1]


        self.biases = process.preprocess_adj_bias(adj)

        train_mask=train_masks
        val_mask=test_masks
        test_mask=test_masks

        self.index_id_dict = self.dataset.network.get_indexer(N_TYPE_NODE).index_id_dict
        self.features = feature_matrix[np.newaxis]

        self.train_mask = train_mask[np.newaxis]
        self.val_mask = val_mask[np.newaxis]
        self.test_mask = test_mask[np.newaxis]


        self.env = Environment(folder_path+"/graph.pbtxt",devices,folder_path)
        self.average_reward=0
        self.best_reward = 1-sys.maxsize
        self.best_replica_num = list()
        self.best_replica_n_hot_num = np.zeros(shape=(self.nb_nodes, len(devices)), dtype=np.int32)
        self.best_device_choice = np.zeros(shape=(self.nb_nodes, len(devices)), dtype=np.int32)
        self.best_ps_or_reduce = list()
        self.folder_path = folder_path
        self.strategy_pool = strategy_pool(folder_path,self.nb_nodes,self.index_id_dict,self.env)

        self.mems=[np.zeros([2, self.nb_nodes, 64], dtype=np.float32) for layer in range(3)]


    def set_session_and_network(self,sess,place_gnn):
        self.sess =sess
        self.place_gnn = place_gnn

    def sample_and_train(self,epoch):
        co_entropy = 0

        tr_step = 0
        tr_size = self.features.shape[0]
        replica_n_hot_nums = list()
        device_choices = list()
        rewards = list()
        ps_or_reduces=list()
        outputs = self.place_gnn.get_replica_num_prob_and_entropy(
            ftr_in=self.features[tr_step * batch_size:(tr_step + 1) * batch_size],
            bias_in=self.biases,
            nb_nodes=self.nb_nodes,
            mems=self.mems)

        for i in range(sample_times):
            replica_num = list()
            replica_n_hot_num = np.zeros(shape=(self.nb_nodes,len(devices)),dtype=np.int32)
            device_choice = np.zeros(shape=(self.nb_nodes,len(devices)),dtype=np.int32)
            ps_or_reduce=list()
            finished_node = list()
            for i in range(len(devices)):
                output = outputs[i]
                for j,pred in enumerate(output):
                   # pred = pred*0.9+(0.1/pred.size)
                    if j not in finished_node:
                        index = np.random.choice(pred.size,p=pred)
                        if index==len(devices):
                            finished_node.append(j)
                            device_choice[j, i] = index
                            replica_n_hot_num[j,i]=1
                        else:
                            device_choice[j,i] = index
                            replica_n_hot_num[j,i]=1
                    else:
                        device_choice[j,i] = -1
            device_choices.append(device_choice)
            replica_n_hot_nums.append(replica_n_hot_num)

            for j, pred in enumerate(outputs[len(devices)]):
                #pred = pred * 0.9 + (0.1 / pred.size)
                index = np.random.choice(pred.size, p=pred)
                ps_or_reduce.append(index)
            ps_or_reduce = np.array(ps_or_reduce)
            ps_or_reduces.append(ps_or_reduce)
            cal_entropy = outputs[-1]
            _reward = self.env.get_reward2( device_choice,ps_or_reduce,self.index_id_dict)
            if _reward>self.best_reward:
                self.best_reward = _reward
                self.best_replica_num = replica_num
                self.best_replica_n_hot_num = replica_n_hot_num
                self.best_device_choice = device_choice
                self.best_ps_or_reduce = ps_or_reduce
            rewards.append(_reward)

        index  = rewards.index(max(rewards))
        self.strategy_pool.insert(rewards[index],device_choices[index],ps_or_reduces[index])
        pool_strategy = self.strategy_pool.choose_strategy()
        rewards.append(pool_strategy["reward"])
        device_choices.append(pool_strategy["device_choice"])
        ps_or_reduces.append(np.reshape(pool_strategy["ps_or_reduce"],(self.nb_nodes,)))

        #sample real distribution
        replica_num = list()
        replica_n_hot_num = np.zeros(shape=(self.nb_nodes,len(devices)),dtype=np.int32)
        device_choice = np.zeros(shape=(self.nb_nodes,len(devices)),dtype=np.int32)
        ps_or_reduce=list()
        finished_node = list()
        for i in range(len(devices)):
            output = outputs[i]
            for j,pred in enumerate(output):
                if j not in finished_node:
                    index = np.random.choice(pred.size,p=pred)
                    if index==len(devices):
                        finished_node.append(j)
                        device_choice[j, i] = index
                        replica_n_hot_num[j,i]=1
                    else:
                        device_choice[j,i] = index
                        replica_n_hot_num[j,i]=1
                else:
                    device_choice[j,i] = -1
        for j, pred in enumerate(outputs[len(devices)]):
            index = np.random.choice(pred.size, p=pred)
            ps_or_reduce.append(index)
        ps_or_reduce = np.array(ps_or_reduce)
        _reward = self.env.get_reward2( device_choice,ps_or_reduce,self.index_id_dict)
        if _reward>self.best_reward:
            self.best_reward = _reward
            self.best_replica_num = replica_num
            self.best_replica_n_hot_num = replica_n_hot_num
            self.best_device_choice = device_choice
            self.best_ps_or_reduce = ps_or_reduce
        if epoch>20:
            self.average_reward = (self.average_reward*19+np.mean(rewards))/20
        else:
            self.average_reward = (self.average_reward * epoch+ np.mean(rewards)) /(epoch+1)


        while tr_step * batch_size < tr_size:
            new_loss,self.mems=self.place_gnn.learn(ftr_in=self.features[tr_step * batch_size:(tr_step + 1) * batch_size],
                            bias_in=self.biases,
                            nb_nodes=self.nb_nodes,
                            replica_num_array=np.array(replica_n_hot_nums),
                            sample_ps_or_reduce = np.array(ps_or_reduces),
                            sample_device_choice = np.array(device_choices),
                            time_ratio = (np.array(rewards)-float(self.average_reward)),
                            coef_entropy=co_entropy,
                            mems = self.mems)
            tr_step += 1
        times = _reward*_reward
        if epoch % show_interval == 0:
            print("[{}] step = {}".format(self.folder_path,epoch))
            print("[{}] time = {}".format(self.folder_path,times))
            print("[{}] average reward = {}".format(self.folder_path,self.average_reward))
            print("[{}] overall entropy:{}".format(self.folder_path,cal_entropy))
            with open(self.folder_path+"/time.log", "a+") as f:
                f.write(str(times) + ",")
            with open(self.folder_path+"/entropy.log", "a+") as f:
                f.write(str(cal_entropy) + ",")
            with open(self.folder_path+"/loss.log", "a+") as f:
                f.write(str(new_loss) + ",")

'''
class ac_feature_item(object):
    def __init__(self,folder_path):
        self.dataset = load_cora(folder_path,NewWhiteSpaceTokenizer())
        adj = self.dataset.adj_matrix(sparse=True)
        feature_matrix, feature_masks = self.dataset.feature_matrix(bag_of_words=False, sparse=False)

        feature_matrix = StandardScaler().fit_transform(feature_matrix)

        labels, label_masks = self.dataset.label_list_or_matrix(one_hot=False)

        train_node_indices, test_node_indices, train_masks, test_masks = self.dataset.split_train_and_test(training_rate=0.8)
        self.features = feature_matrix

        self.nb_nodes = self.features.shape[0]

        self.ft_size = self.features.shape[1]+len(devices)+1


        self.biases = process.preprocess_adj_bias(adj)

        train_mask=train_masks
        val_mask=test_masks
        test_mask=test_masks

        self.index_id_dict = self.dataset.network.get_indexer(N_TYPE_NODE).index_id_dict

        self.train_mask = train_mask[np.newaxis]
        self.val_mask = val_mask[np.newaxis]
        self.test_mask = test_mask[np.newaxis]


        self.env = Environment(folder_path+"/graph.pbtxt",devices,folder_path)
        self.average_reward=0
        self.best_reward = 1-sys.maxsize
        self.best_replica_num = list()
        self.best_replica_n_hot_num = np.zeros(shape=(self.nb_nodes, len(devices)), dtype=np.int32)
        self.best_device_choice = np.zeros(shape=(self.nb_nodes, len(devices)), dtype=np.int32)
        self.best_ps_or_reduce = list()
        self.folder_path = folder_path

        if os.path.exists(folder_path+"/current_state.json"):
            with open(folder_path+"/current_state.json","r") as f:
                tmp = json.load(f)
                self.current_state = np.array(tmp)
        else:
            self.current_state = np.ones(shape=(self.nb_nodes,len(devices)+1))
            with open(folder_path + "/current_state.json", "w") as f:
                json.dump(self.current_state.tolist(),f)


    def set_session_and_network(self,sess,actor,critic):
        self.sess =sess
        self.actor = actor
        self.critic = critic

    def sample_and_train(self,epoch):
        co_entropy = 1
        rewards = list()

        replica_num = list()
        replica_n_hot_num = np.zeros(shape=(self.nb_nodes,len(devices)),dtype=np.int32)
        device_choice = np.zeros(shape=(self.nb_nodes,len(devices)),dtype=np.int32)
        ps_or_reduce=list()
        current_ftr_in = np.concatenate((self.features,self.current_state),axis=1)
        outputs = self.actor.get_replica_num_prob_and_entropy(ftr_in=current_ftr_in[np.newaxis],
                                                                  bias_in=self.biases,
                                                             nb_nodes=self.nb_nodes)

        finished_node = list()
        for i in range(len(devices)):
            output = outputs[i]
            for j,pred in enumerate(output):
                if j not in finished_node:
                    index = np.random.choice(pred.size,p=pred)
                    if index==len(devices):
                        finished_node.append(j)
                        device_choice[j, i] = index
                        replica_n_hot_num[j,i]=1
                    else:
                        device_choice[j,i] = index
                        replica_n_hot_num[j,i]=1
                else:
                    device_choice[j,i] = -1

        for j, pred in enumerate(outputs[len(devices)]):
            index = np.random.choice(pred.size, p=pred)
            ps_or_reduce.append(index)
        ps_or_reduce = np.array(ps_or_reduce)

        cal_entropy = outputs[-1]
        _reward = self.env.get_reward2( device_choice,ps_or_reduce,self.index_id_dict)

        next_state = self.env.get_stratey_array(device_choice, ps_or_reduce)
        next_ftr_in = np.concatenate((self.features,next_state),axis=1)
        if _reward>self.best_reward:
            self.best_reward = _reward
            self.best_replica_num = replica_num
            self.best_replica_n_hot_num = replica_n_hot_num
            self.best_device_choice = device_choice
            self.best_ps_or_reduce = ps_or_reduce


        rewards.append(_reward)
        if epoch>20:
            self.average_reward = (self.average_reward*19+np.mean(rewards))/20
        else:
            self.average_reward = (self.average_reward * epoch+ np.mean(rewards)) /(epoch+1)


        td_error = self.critic.learn(current_ftr_in[np.newaxis], next_ftr_in[np.newaxis],_reward,self.biases, self.nb_nodes)
        new_loss=self.actor.learn(ftr_in=current_ftr_in[np.newaxis],
                        bias_in=self.biases,
                        nb_nodes=self.nb_nodes,
                        replica_num_array=np.array(replica_n_hot_num),
                        sample_ps_or_reduce = np.array(ps_or_reduce),
                        sample_device_choice = np.array(device_choice),
                        time_ratio = (td_error),
                        coef_entropy=co_entropy)
        times = [item*item for item in rewards]
        if epoch % show_interval == 0:
            print("[{}] step = {}".format(self.folder_path,epoch))
            print("[{}] time = {}".format(self.folder_path,times))
            print("[{}] average reward = {}".format(self.folder_path,self.average_reward))
            print("[{}] overall entropy:{}".format(self.folder_path,cal_entropy))
            with open(self.folder_path+"/time.log", "a+") as f:
                f.write(str(np.mean(times)) + ",")
            with open(self.folder_path+"/entropy.log", "a+") as f:
                f.write(str(cal_entropy) + ",")
            with open(self.folder_path+"/loss.log", "a+") as f:
                f.write(str(new_loss) + ",")

'''

class new_place_GNN():
    def __init__(self,sess,ft_size):
        with tf.name_scope('place_gnn'):
            self.sess = sess

            self.ftr_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, None, ft_size),name="ftr_in")
            #self.bias_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, None, None),name="bias_in")
            self.bias_in = tf.sparse_placeholder(dtype=tf.float32)
            self.nb_node = tf.placeholder(dtype=tf.int32, shape=(),name="nb_node")
            self.attn_drop = tf.placeholder(dtype=tf.float32, shape=(),name="attn_drop")
            self.ffd_drop = tf.placeholder(dtype=tf.float32, shape=(),name="ffd_drop")
            self.is_train = tf.placeholder(dtype=tf.bool, shape=(),name="is_train")
            self.sample_ps_or_reduce = tf.placeholder(dtype=tf.int32, shape=(None,None,),name="sample_ps_or_reduce")
            self.sample_device_choice = tf.placeholder(dtype=tf.int32, shape=(None,None,len(devices),),name="sample_device_choice")
            self.replica_num_array = tf.placeholder(dtype=tf.float32, shape=(None,None,len(devices)),name="replica_num_array")
            self.time_ratio = tf.placeholder(dtype=tf.float32, shape=(None,),name="time_ratio")
            self.coef_entropy = tf.placeholder(dtype=tf.float32, shape=(),name="coef_entropy")
            self.mems = [tf.placeholder(tf.float32,[2, None, 64]) for _ in range(3)]

        logits = model.inference(self.ftr_in, 64, self.nb_node, self.is_train,
                                 self.attn_drop, self.ffd_drop,
                                 bias_mat=self.bias_in,
                                 hid_units=hid_units, n_heads=n_heads,
                                 residual=residual, activation=nonlinearity)
        log_resh = tf.reshape(logits, [-1, 64])
        log_resh = tf.layers.dense(log_resh, units=64, activation=tf.nn.relu)
        if not transformer:
            self.cell = tf.nn.rnn_cell.MultiRNNCell([self.get_a_cell() for _ in range(len(devices))])
            h0 = self.cell.zero_state(self.nb_node, np.float32)
            self.output,self.h = self.cell.call(log_resh, h0)
            self.ps_or_reduce = tf.layers.dense(self.output, units=2, activation=tf.nn.softmax)
            self.device_choices = list()
            sum=0
            out = tf.layers.dense(self.h[0].c, units=len(devices), activation=tf.nn.softmax)
            self.device_choices.append(out)
            sum = sum + tf.reduce_mean(tf.reduce_sum(tf.log(out + np.power(10.0, -9)) * out, 1))
            for i in range(len(devices)-1):
                out = tf.layers.dense(self.h[i+1].c, units=len(devices)+1, activation=tf.nn.softmax)
                self.device_choices.append(out)
                sum = sum+tf.reduce_mean(tf.reduce_sum(tf.log(out + np.power(10.0, -9)) *out, 1))
            self.entropy = tf.reduce_sum(tf.log(self.ps_or_reduce + np.power(10.0, -9)) * self.ps_or_reduce, 1)
            self.entropy = -(tf.reduce_mean(self.entropy) + sum / len(devices))
        else:
            self.device_choices = list()
            for i in range(3):
                output = rel_multihead_attn(
                    w=log_resh,
                    r = self.bias_in,
                    d_model=64,
                    mems=self.mems[i],
                    n_head=10,
                    d_head=50,
                    nb_nodes=self.nb_node)
                output = tf.layers.dense(output, len(devices)*(len(devices)+1)+1, activation=tf.nn.relu)
            sum=0
            o1 = tf.nn.softmax(output[:,0:len(devices)])
            self.device_choices.append(o1)
            sum = sum + tf.reduce_mean(tf.reduce_sum(tf.log(o1 + np.power(10.0, -9)) * o1, 1))
            for i in range(1,len(devices)):
                oi = tf.nn.softmax(output[:,i*(len(devices)+1)-1:(i+1)*(len(devices)+1)-1])
                self.device_choices.append(oi)
                sum = sum + tf.reduce_mean(tf.reduce_sum(tf.log(oi + np.power(10.0, -9)) * oi, 1))
            self.ps_or_reduce = tf.nn.softmax(output[:,-2:])
            self.entropy = tf.reduce_sum(tf.log(self.ps_or_reduce + np.power(10.0, -9)) * self.ps_or_reduce, 1)
            self.entropy = -(tf.reduce_mean(self.entropy) + sum / len(devices))


        for i in range(sample_times):
            one_hot_sample = tf.one_hot(self.sample_ps_or_reduce[i], 2)
            print("one_hot_sample.shape")
            print(one_hot_sample.shape)

            prob = tf.reduce_sum( self.ps_or_reduce * one_hot_sample, 1)
            # prob = tf.reduce_prod(peirob)
            reward = tf.reduce_sum(tf.log(prob + np.power(10.0, -9)) * self.time_ratio[i])

            #first device choice n*m
            one_hot_sample = tf.one_hot(self.sample_device_choice[i][:, 0], len(devices))
            prob = tf.reduce_sum(self.device_choices[0] * one_hot_sample, 1) * self.replica_num_array[i][:, 0] + (
                        1 - self.replica_num_array[i][:, 0])
            reward += tf.reduce_sum(tf.log(prob + np.power(10.0, -9)) * self.time_ratio[i])

            #rest device choice n*(m+1)
            for j in range(1,len(devices)):
                one_hot_sample = tf.one_hot(self.sample_device_choice[i][:,j], len(devices)+1)
                prob = tf.reduce_sum(self.device_choices[j] * one_hot_sample, 1) * self.replica_num_array[i][:,j]+(1-self.replica_num_array[i][:,j])
                reward+=tf.reduce_sum(tf.log(prob + np.power(10.0, -9)) * self.time_ratio[i])


        reward = reward/sample_times + self.coef_entropy * self.entropy
        self.loss = -reward
        self.train_op = model.training(self.loss, lr, l2_coef)



    def get_a_cell(self):
        return tf.nn.rnn_cell.BasicLSTMCell(num_units=64)

    def learn(self,ftr_in,bias_in,nb_nodes,replica_num_array,sample_ps_or_reduce,sample_device_choice,time_ratio,coef_entropy,mems):
        feed_dict = {}
        feed_dict[self.ftr_in]=ftr_in
        feed_dict[self.bias_in]=bias_in
        feed_dict[self.nb_node]=nb_nodes
        feed_dict[self.is_train]=True
        feed_dict[self.attn_drop]=0.1
        feed_dict[self.ffd_drop]=0.1
        feed_dict[self.replica_num_array]=replica_num_array
        feed_dict[self.sample_ps_or_reduce]=sample_ps_or_reduce
        feed_dict[self.sample_device_choice]=sample_device_choice
        feed_dict[self.time_ratio]=time_ratio
        feed_dict[self.coef_entropy]=coef_entropy
        for item1,item2 in zip(self.mems,mems):
            feed_dict[item1]=item2
        loss,mems,_ = self.sess.run([self.loss,self.mems,self.train_op],
                     feed_dict=feed_dict)
        return loss,mems
    def get_replica_num_prob_and_entropy(self,ftr_in,bias_in,nb_nodes,mems):
        fetch_list =[item for item in self.device_choices]
        fetch_list.append(self.ps_or_reduce)
        fetch_list.append(self.entropy)


        feed_dict = {}
        feed_dict[self.ftr_in]=ftr_in
        feed_dict[self.bias_in]=bias_in
        feed_dict[self.nb_node]=nb_nodes
        feed_dict[self.is_train]=True
        feed_dict[self.attn_drop]=0.1
        feed_dict[self.ffd_drop]=0.1
        for item1,item2 in zip(self.mems,mems):
            feed_dict[item1]=item2

        outputs = self.sess.run(fetch_list, feed_dict=feed_dict)
        return outputs



'''

class actor():
    def __init__(self,sess,ft_size):
        with tf.name_scope('place_gnn'):
            self.sess = sess

            self.ftr_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, None, ft_size),name="ftr_in")
            #self.bias_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, None, None),name="bias_in")
            self.bias_in = tf.sparse_placeholder(dtype=tf.float32)
            self.nb_node = tf.placeholder(dtype=tf.int32, shape=(),name="nb_node")
            self.attn_drop = tf.placeholder(dtype=tf.float32, shape=(),name="attn_drop")
            self.ffd_drop = tf.placeholder(dtype=tf.float32, shape=(),name="ffd_drop")
            self.is_train = tf.placeholder(dtype=tf.bool, shape=(),name="is_train")
            self.sample_ps_or_reduce = tf.placeholder(dtype=tf.int32, shape=(None,),name="sample_ps_or_reduce")
            self.sample_device_choice = tf.placeholder(dtype=tf.int32, shape=(None,len(devices),),name="sample_device_choice")
            self.replica_num_array = tf.placeholder(dtype=tf.float32, shape=(None,len(devices)),name="replica_num_array")
            self.time_ratio = tf.placeholder(dtype=tf.float32, shape=(),name="time_ratio")
            self.coef_entropy = tf.placeholder(dtype=tf.float32, shape=(),name="coef_entropy")

        logits = model.inference(self.ftr_in, 64, self.nb_node, self.is_train,
                                 self.attn_drop, self.ffd_drop,
                                 bias_mat=self.bias_in,
                                 hid_units=hid_units, n_heads=n_heads,
                                 residual=residual, activation=nonlinearity)
        log_resh = tf.reshape(logits, [-1, 64])
        log_resh = tf.layers.dense(log_resh, units=64, activation=tf.nn.relu)
        self.cell = tf.nn.rnn_cell.MultiRNNCell([self.get_a_cell() for _ in range(len(devices))])
        h0 = self.cell.zero_state(self.nb_node, np.float32)
        self.output,self.h = self.cell.call(log_resh, h0)
        self.ps_or_reduce = tf.layers.dense(self.output, units=2, activation=tf.nn.softmax)
        self.device_choices = list()
        sum=0
        out = tf.layers.dense(self.h[0].c, units=len(devices), activation=tf.nn.softmax)
        self.device_choices.append(out)
        sum = sum + tf.reduce_mean(tf.reduce_sum(tf.log(out + np.power(10.0, -9)) * out, 1))
        for i in range(len(devices)-1):
            out = tf.layers.dense(self.h[i+1].c, units=len(devices)+1, activation=tf.nn.softmax)
            self.device_choices.append(out)
            sum = sum+tf.reduce_mean(tf.reduce_sum(tf.log(out + np.power(10.0, -9)) *out, 1))

        self.entropy = tf.reduce_sum(tf.log(self.ps_or_reduce + np.power(10.0, -9)) * self.ps_or_reduce, 1)
        self.entropy = tf.reduce_mean(self.entropy)+sum/len(devices)

        one_hot_sample = tf.one_hot(self.sample_ps_or_reduce, 2)
        print("one_hot_sample.shape")
        print(one_hot_sample.shape)

        prob = tf.reduce_sum(self.ps_or_reduce * one_hot_sample, 1)
        # prob = tf.reduce_prod(peirob)
        reward = tf.reduce_sum(tf.log(prob + np.power(10.0, -9)) * self.time_ratio)

        #first device choice n*m
        one_hot_sample = tf.one_hot(self.sample_device_choice[:, 0], len(devices))
        prob = tf.reduce_sum(self.device_choices[0] * one_hot_sample, 1) * self.replica_num_array[:, 0] + (
                1 - self.replica_num_array[:, 0])
        reward += tf.reduce_sum(tf.log(prob + np.power(10.0, -9)) * self.time_ratio)

        #rest device choice n*(m+1)
        for j in range(1,len(devices)):
            one_hot_sample = tf.one_hot(self.sample_device_choice[:,j], len(devices)+1)
            prob =  tf.reduce_sum(self.device_choices[j] * one_hot_sample, 1) * self.replica_num_array[:,j]+(1-self.replica_num_array[:,j])
            reward+=tf.reduce_sum(tf.log(prob + np.power(10.0, -9)) * self.time_ratio)


        self.loss = -reward #+ self.coef_entropy * self.entropy
        self.train_op = model.training(self.loss, lr, l2_coef)



    def get_a_cell(self):
        return tf.nn.rnn_cell.BasicLSTMCell(num_units=64)

    def learn(self,ftr_in,bias_in,nb_nodes,replica_num_array,sample_ps_or_reduce,sample_device_choice,time_ratio,coef_entropy):
        loss,_ = self.sess.run([self.loss,self.train_op],
                     feed_dict={
                         self.ftr_in: ftr_in,
                         self.bias_in:bias_in,
                         self.nb_node:nb_nodes,
                         self.is_train: True,
                         self.attn_drop: 0.1, self.ffd_drop: 0.1, self.replica_num_array: replica_num_array,
                         self.sample_ps_or_reduce:sample_ps_or_reduce,
                         self.sample_device_choice:sample_device_choice,
                         self.time_ratio:time_ratio, self.coef_entropy: coef_entropy})
        return loss
    def get_replica_num_prob_and_entropy(self,ftr_in,bias_in,nb_nodes):
        fetch_list =[item for item in self.device_choices]
        fetch_list.append(self.ps_or_reduce)
        fetch_list.append(self.entropy)
        outputs = self.sess.run(fetch_list, feed_dict={
            self.ftr_in: ftr_in,
            self.bias_in: bias_in,
            self.nb_node:nb_nodes,
            self.is_train: True,
            self.attn_drop: 0.1, self.ffd_drop: 0.1})
        return outputs
class critic():
    def __init__(self, sess, ft_size):
        with tf.name_scope('critic'):
            self.sess = sess

            self.ftr_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, None, ft_size), name="ftr_in")
            # self.bias_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, None, None),name="bias_in")
            self.bias_in = tf.sparse_placeholder(dtype=tf.float32)
            self.nb_node = tf.placeholder(dtype=tf.int32, shape=(), name="nb_node")
            self.attn_drop = tf.placeholder(dtype=tf.float32, shape=(), name="attn_drop")
            self.ffd_drop = tf.placeholder(dtype=tf.float32, shape=(), name="ffd_drop")
            self.is_train = tf.placeholder(dtype=tf.bool, shape=(), name="is_train")
            self.v_ = tf.placeholder(dtype=tf.float32, shape=(), name="v_next")
            self.r = tf.placeholder(dtype=tf.float32, shape=(), name='r')


        logits = model.inference(self.ftr_in, 64, self.nb_node, self.is_train,
                                 self.attn_drop, self.ffd_drop,
                                 bias_mat=self.bias_in,
                                 hid_units=hid_units, n_heads=n_heads,
                                 residual=residual, activation=nonlinearity)
        log_resh = tf.reshape(logits, [-1, 64])
        log_resh = tf.layers.dense(log_resh, units=1, activation=tf.nn.relu)
        self.v = tf.reduce_mean(log_resh)
        self.td_error = self.r+0.9*self.v_-self.v
        self.loss = tf.square(self.td_error)
        self.train_op = model.training(self.loss, lr, l2_coef)

    def get_a_cell(self):
        return tf.nn.rnn_cell.BasicLSTMCell(num_units=64)

    def learn(self, ftr_in, next_ftr_in,reward,bias_in, nb_nodes):
        next_v = self.sess.run(self.v,feed_dict={
                                    self.ftr_in: next_ftr_in,
                                    self.bias_in: bias_in,
                                    self.nb_node: nb_nodes,
                                    self.is_train: True,
                                    self.attn_drop: 0.1, self.ffd_drop: 0.1})

        td_error, _ = self.sess.run([self.td_error, self.train_op],
                                feed_dict={
                                    self.ftr_in: ftr_in,
                                    self.bias_in: bias_in,
                                    self.nb_node: nb_nodes,
                                    self.is_train: True,
                                    self.attn_drop: 0.1, self.ffd_drop: 0.1,self.r:reward,self.v_:next_v})
        return td_error

    def get_replica_num_prob_and_entropy(self, ftr_in, bias_in, nb_nodes):
        fetch_list = [item for item in self.device_choices]
        fetch_list.append(self.ps_or_reduce)
        fetch_list.append(self.entropy)
        outputs = self.sess.run(fetch_list, feed_dict={
            self.ftr_in: ftr_in,
            self.bias_in: bias_in,
            self.nb_node: nb_nodes,
            self.is_train: True,
            self.attn_drop: 0.1, self.ffd_drop: 0.1})
        return outputs
'''
def architecture_three():
    models = list()
    for feature_folder in feature_folders:
        models.append(feature_item(folder_path=feature_folder))
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    with tf.Session(config=config) as sess:
        place_gnn = new_place_GNN(sess,ft_size=models[0].ft_size)

        for model in models:
            model.set_session_and_network(sess,place_gnn)

        saver = tf.train.Saver()
        try:
            saver.restore(sess, checkpt_file)
        except:
            init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
            sess.run(init_op)

        for epoch in range(nb_epochs):

            for model in models:
                model.sample_and_train(epoch)

            if epoch % show_interval == 0:
                saver.save(sess, checkpt_file)

        sess.close()
'''
def actor_critic():
    models = list()
    for feature_folder in feature_folders:
        models.append(ac_feature_item(folder_path=feature_folder))
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    with tf.Session(config=config) as sess:
        _actor = actor(sess,ft_size=models[0].ft_size)
        _critic = critic(sess,ft_size=models[0].ft_size)

        for model in models:
            model.set_session_and_network(sess,_actor,_critic)

        saver = tf.train.Saver()
        try:
            saver.restore(sess, checkpt_file)
        except:
            init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
            sess.run(init_op)

        for epoch in range(nb_epochs):

            for model in models:
                model.sample_and_train(epoch)

            if epoch % show_interval == 0:
                saver.save(sess, checkpt_file)

        sess.close()
'''
architecture_three()