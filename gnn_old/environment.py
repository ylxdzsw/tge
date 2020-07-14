from tge import TGE
import numpy as np
from sklearn.cluster import KMeans

import sys
def info(*args):
    print(*args, file=sys.stdout, flush=True)

def sample(logp):
    mask = np.zeros(logp.shape, dtype=bool)
    result = np.zeros(logp.shape[:-1], dtype=int)
    for i in range(logp.shape[0]):
        for j in range(logp.shape[1]):
            s = int(np.random.choice(logp.shape[2], p=np.exp(logp[i, j, :])))
            mask[i, j, s] = 1
            result[i, j] = s
    return mask, result

def kmeans_sample(logp, k=8):
    logp_expanded = np.reshape(logp, (logp.shape[0], -1))
    kmeans = KMeans(k).fit(np.exp(logp_expanded))
    groups = kmeans.labels_
    choices = {}
    mask = np.zeros(logp.shape, dtype=bool)
    result = np.zeros(logp.shape[:-1], dtype=int)
    for i in range(logp.shape[0]):
        gi = groups[i]
        if gi in choices:
            choice = choices[gi]
        else:
            choice = [int(np.random.choice(logp.shape[2], p=np.exp(logp[i, j, :]))) for j in range(logp.shape[1])]
            choices[gi] = choice
        for j in range(logp.shape[1]):
            s = choice[j]
            mask[i, j, s] = 1
            result[i, j] = s
    return mask, result

def explore(logp):
    mask = np.zeros(logp.shape, dtype=bool)
    result = np.zeros(logp.shape[:-1], dtype=int)
    for j in range(logp.shape[1]):
        s = int(np.random.choice(logp.shape[2]))
        for i in range(logp.shape[0]):
            mask[i, j, s] = 1
            result[i, j] = s
    return mask, result

def crossover(logp, d1, d2):
    mask = np.zeros(logp.shape, dtype=bool)
    result = np.zeros(logp.shape[:-1], dtype=int)
    for i in range(logp.shape[0]):
        if np.random.rand() < 0.8:
            d = d1
        else:
            d = d2
        for j in range(logp.shape[1]):
            s = d[i, j]
            mask[i, j, s] = 1
            result[i, j] = s
    return mask, result

def evaluate(record, decisions):
    gdef = record["gdef"]
    if record["groups"] is not None:
        strategy = { gdef.node[i].name: [2, *decisions[gi]] for gi, group in enumerate(record["groups"]) for i in group }
    else:
        strategy = { gdef.node[i].name: [2, *decisions[i]] for i in range(decisions.shape[0]) }
    penalty = 1
    for k, v in strategy.items():
        if np.sum(v[1:]) == 0:
            penalty += 1
            v[1] = 1
    tge = TGE(gdef, [dev for dev, _, _ in record["devices"]])
    tge.set_strategy(strategy)
    tge.fill_batchsize(48)
    # tge.use_collective()
    tge.set_bandwidth(intra=record["intra"], inter=record["inter"])
    time, mem = tge.evaluate(record["prof_data"])

    for m, (_, limit, _) in zip(mem, record["devices"]):
        if m > limit:
            penalty += 1

    return (time / 1000000) * penalty ** .5

def predict_and_evaluate(record, logp):
    decisions = np.argmax(logp, axis=2)
    loss = evaluate(record, decisions)
    mask = np.zeros(logp.shape, dtype=bool)
    for i in range(logp.shape[0]):
        for j in range(logp.shape[1]):
            s = decisions[i, j]
            mask[i, j, s] = 1
    return mask, loss

def sample_and_evaluate(record, logp):
    mask, decisions = sample(logp)
    loss = evaluate(record, decisions)
    return mask, loss

def explore_and_evaluate(record, logp):
    mask, decisions = explore(logp)
    loss = evaluate(record, decisions)
    return mask, loss

def modify_and_evaluate(record, origin_mask):
    _, explored = explore(origin_mask)
    mask, decisions = crossover(origin_mask, np.argmax(origin_mask, 2), explored)
    loss = evaluate(record, decisions)
    return mask, loss

def evaluate_logp(record, logp, ppredict=.2, nsample=1, nexplore=1, nmodify=4, poolsize=8):
    if "pool" not in record:
        record["pool"] = [explore_and_evaluate(record, logp) for _ in range(poolsize)]
        nexplore += 20 * logp.shape[1] * poolsize
        nmodify += 20 * logp.shape[1] * poolsize
    pool = record["pool"]

    if np.random.rand() < ppredict:
        mask, loss = predict_and_evaluate(record, logp)
        i = np.random.choice(poolsize)
        if pool[i][1] > loss:
            pool[i] = mask, loss

    for _ in range(nsample):
        mask, loss = sample_and_evaluate(record, logp)
        i = np.random.choice(poolsize)
        if pool[i][1] > loss:
            pool[i] = mask, loss

    for _ in range(nexplore):
        mask, loss = explore_and_evaluate(record, logp)
        i = np.random.choice(poolsize)
        if pool[i][1] > loss:
            pool[i] = mask, loss

    for _ in range(nmodify):
        i = np.random.choice(poolsize)
        mask, loss = modify_and_evaluate(record, pool[i][0])
        if pool[i][1] > loss:
            pool[i] = mask, loss

    i = np.random.choice(len(pool))

    return pool[i]
