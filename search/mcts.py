import numpy as np
import itertools
import copy
import tge
from dataclasses import dataclass
from typing import Any
from data import TopoSpec, TopoSpecTask, ProfileData, device_name, gen_topology_for_simulator, gen_nccl_model, gen_data
from grouping import group_with_topk_nodes, group_with_tge_basegroups
from utils import info, load
from metis import metis
from environment import evaluate_with_feedback, invalidity

@dataclass
class Action:
    placement: Any # a list of the same length of machines
    communication: Any # 0: PS, 1: NCCL, 2: MP

    def to_mask(self):
        placement_mask = np.expand_dims(np.array(self.placement_mask), 1)
        communication_mask = np.zeros((1, 3))
        communication_mask[0, self.communication] = 1
        return placement_mask, communication_mask

@dataclass
class State:
    record: Any
    sorted_groups_indices: Any
    sorted_groups: Any
    dp_time: Any
    actions: Any # the actions taken so far. The rest nodes uses the first action (same strategy as the most computational expensive group)
    feedback: Any

    # shallow copy except for the actions
    def clone(self):
        x = copy.copy(self)
        x.actions = copy.deepcopy(self.actions)
        return x

    def finished(self):
        return len(self.actions) >= len(self.sorted_groups)

    def fill_cache(self):
        gdef, topo_spec, prof_data, batchsize = self.record['gdef'], self.record['topo_spec'], self.record['prof_data'], self.record['batchsize']
        self.sorted_groups_indices = sorted(list(range(len(self.record['op_groups']))), key=lambda i: -np.sum([ prof_data.get('1080ti', batchsize)[gdef.node[node_id].name] for node_id in self.record['op_groups'][i] ])) # largest computation time first
        self.sorted_groups = [ self.record['op_groups'][i] for i in self.sorted_groups_indices ]

        state_copy = self.clone()
        state_copy.actions.append(([1 for _ in range(len(topo_spec.tasks))], 1))
        time, _ = evaluate_with_feedback(state_copy)

        # TODO: if OOM, use MP as baseline
        # TODO: save to record
        self.dp_time = time
        return self

    def get_action(self, i):
        if i < len(self.actions):
            return self.actions[i]
        else:
            return self.acitons[0]

    @staticmethod
    def new(record):
        return State(record, None, None, 0, [], None).fill_cache()

class Node:
    def __init__(self, action):
        self.action = action
        self.p = 0
        self.q = 0
        self.n_visits = 0
        self.children = []
        self.value = None

    def playout_and_update_recursive(self, state, options):
        if self.is_leaf():
            if not state.finished():
                self.expand(state, options)
            if len(state.actions) == 0: # root at first
                return 0.
            leaf_value = self.evaluate(state)
            self.update(leaf_value)

            return leaf_value
        child = self.select_child()
        state.actions.append(child.action)
        leaf_value = child.playout_and_update_recursive(state, options)
        self.update(leaf_value)
        return leaf_value

    def is_leaf(self):
        return len(self.children) == 0

    def select_child(self):
        return max(self.children, key=lambda x: x.puct(self.n_visits))

    def puct(self, pvisit):
        return self.q + 1.4 * self.p * np.sqrt(pvisit) / (1 + self.n_visits)

    def update(self, leaf_value):
        self.n_visits += 1
        self.q += (leaf_value - self.q) / self.n_visits

    def expand(self, state, options):
        for placement in itertools.product([0, 1], repeat=len(state.record['topo_spec'].tasks)):
            if sum(placement) == 0:
                continue

            ndevices = sum( state.record['topo_spec'].tasks[i].number for i in placement if i == 1 )

            for communication in range(3):
                if ndevices == 1 and communication != 0:
                    continue

                if options.real_topo and state.record['batchsize'] % ndevices != 0 and communication != 2:
                    continue

                action = placement, communication
                child = Node(action)
                if len(state.actions) > 0 and action == state.actions[0]: # TODO: should we do this?
                    child.n_visits += self.n_visits

                self.children.append(child)

        if options.policy_fun is not None:
            masks = [ child.action.to_mask() for child in self.children ]
            log_softmaxs = policy_fun(state, *zip(*masks))

            for child, log_softmax in zip(self.children, log_softmaxs):
                info(child.action, np.exp(log_softmax))
                child.p = np.exp(log_softmax)
        else:
            for child in self.children:
                child.p = 1 / len(self.children)

    def evaluate(self, state):
        if self.value is None:
            time, feedback = evaluate_with_feedback(state)
            speed_up = -1 if invalidity(state.record, feedback) > 0 else state.dp_time / time - 1
            self.value = speed_up
            state.feedback = feedback
        return self.value

class Tree:
    def __init__(self, policy_fun, real_topo=False): # real_topo controls whether we should filter out the un-dividable replications
        self.policy_fun = policy_fun
        self.real_topo = real_topo
        self.root = Node(None)

    def playout(self, state, ntimes, trace_fun=None):
        best = -1
        best_actions = None
        for n in range(ntimes):
            state_clone = state.clone()
            leaf_value = self.root.playout_and_update_recursive(state_clone, self)
            if leaf_value > best:
                best = leaf_value
                best_actions = state_clone.actions
            if trace_fun is not None:
                trace_fun(leaf_value, state_clone.actions)
        return best, best_actions

    def get_action(self):
        return max(self.root.children, key=lambda x: x.n_visits).action

if __name__ == '__main__':
    import sys

    m = sys.argv[1]

    topo = TopoSpec([
        TopoSpecTask('v100',   12<<30, 8000, 2),
        TopoSpecTask('v100',   12<<30, 8000, 2),
        TopoSpecTask('1080ti', 8<<30, 8000, 2),
    ], [[5000, 2180, 5000],
        [2180, 5000, 5000],
        [5000, 5000, 5000]])

    gdef = load('raw_data/{}/model.pickle'.format(m))
    prof_data = ProfileData(m)
    tge.simplify_graph(gdef, sinks=["Adam"])

    record = gen_data(gdef, prof_data, prof_data.maximum_batchsize(), topo)

    state = State(record, None, 0, []).fill_cache()
    print(Tree(None).playout(state, 800))
