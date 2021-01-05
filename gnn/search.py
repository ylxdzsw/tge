import tensorflow as tf
import numpy as np
from environment import sample, evaluate, f
from utils import save, load, info

from pymoo.model.problem import Problem

from multiprocessing import Pool

pool = Pool(16)

class MyProblem(Problem):
    def __init__(self, record):
        self.record = record
        n = len(record['op_groups']) * len(record['devices']) + len(record['op_groups']) + len(record['op_groups'])
        super().__init__(n_var=n, n_obj=1, n_constr=0, xl=0, xu=[2.999999]*(len(record['op_groups']) * len(record['devices'])) + [1.999999]*len(record['op_groups']) + [len(record["devices"]) - .000001]*len(record['op_groups']) )

    def _evaluate(self, x, out, *args, **kwargs):
        pheno = x.astype(int)

        ks = pool.map(f, [(self.record, pheno[i, :]) for i in range(pheno.shape[0])])

        out["F"] = [[k] for k in ks]
        out["pheno"] = pheno
        # out["hash"] = hash(str(pheno))

from pymoo.algorithms.so_brkga import BRKGA
from pymoo.optimize import minimize

# from pymoo.model.duplicate import ElementwiseDuplicateElimination

# class MyElementwiseDuplicateElimination(ElementwiseDuplicateElimination):
#     def is_equal(self, a, b):
#         info(a.get("hash"))
#         return a.get("hash") == b.get("hash")

from pymoo.model.sampling import Sampling

class MySampling(Sampling):
    def __init__(self, seeds, nodep, ncclp, psp, cap=0.002):
        super().__init__()
        self.seeds = seeds
        self.nodep = nodep * (1 - cap) + 1/3 * cap
        self.ncclp = ncclp * (1 - cap) + 0.5 * cap
        self.psp = psp * (1 - cap) + 1/psp.shape[1] * cap

    def _do(self, problem, n_samples, **kwargs):
        X = np.full((n_samples, problem.n_var), None, dtype=np.float)

        for i in range(n_samples):
            nd = self.psp.shape[1]
            node = np.array([np.random.choice(3, p=self.nodep[j, :]) / 2 * 2.999999 for j in range(self.nodep.shape[0])])
            nccl = np.random.rand(len(self.ncclp)) < self.ncclp
            ps = np.array([np.random.choice(nd, p=self.psp[j, :]) / (nd - 1) * (nd - .000001) for j in range(self.psp.shape[0])])
            X[i, :] = np.hstack([node, nccl, ps])

        if self.seeds is not None:
            for i, seed in enumerate(self.seeds):
                X[i, :] = seed
            self.seeds = None

        # X = np.random.rand(n_samples, problem.n_var)

        return X

def search(record, nodep, ncclp, psp, n_gen=20):
    problem = MyProblem(record)

    seeds = None
    # if 'elites' in record:
    #     seeds = [ np.hstack([np.reshape(nodemask, (-1, )), ncclmask]) for loss_env, nodemask, ncclmask in record['elites'] ]

    algorithm = BRKGA(
        n_elites=64,
        n_offsprings=64,
        n_mutants=16,
        bias=0.8,
        sampling=MySampling(seeds, nodep, ncclp, psp),
        eliminate_duplicates=True)
        # eliminate_duplicates=MyElementwiseDuplicateElimination)

    res = minimize(problem, algorithm, ("n_gen", n_gen), verbose=False)
    nodemask = res.opt.get("pheno")[0][:len(record['op_groups']) * len(record['devices'])]
    ncclmask = res.opt.get("pheno")[0][len(record['op_groups']) * len(record['devices']):-len(record['op_groups'])]
    psmask = res.opt.get("pheno")[0][-len(record['op_groups']):]

    # info("Best solution found: \nX = %s\nF = %s" % (res.X, res.F))
    # info("Solution", sol)

    return res.F[0], nodemask, ncclmask, psmask
