# -*- coding: utf-8 -*-
# @Time    : 2019/12/31 16:25
# @Author  : Matrix
# @Site    :
# @File    : fragmentLengthsDist.py
# @Software: PyCharm
from __future__ import print_function
import sys, os
import pandas as pd
from scipy import *
from scipy import special
from scipy import stats
import copy
import warnings
import pysam
import h5py
import re
from . import signal_track_builder
from numpy import *
from copy import deepcopy


warnings.filterwarnings("ignore")


class EMSmoothFragLenDist(object):
    """
    Smooth the fragment length distribution from 50 to 600bp using a mixture of exponential distribution and three
    Gaussian distributions representing nucleosome free and 1, 2 and 3 nucleosomes.
    Note: here the fragment lengths has been shifted.
    """

    def __init__(self, fl, n_nucleo,nucfree_dist_family='gamma'):
        self.n_nuc = n_nucleo
        self.nums = deepcopy(pd.Series(fl))
        self.fl = deepcopy(pd.Series(fl))
        self.lengthShift = min(self.fl.index)
        self.fl.index = self.fl.index - self.lengthShift
        self.fl = self.fl / float(sum(self.fl))
        self.keys = asarray(self.fl.index) + 0.5
        self.values = asarray(self.fl)
        self.params = [
            1,
            50,
            linspace(200, max(self.nums.index), self.n_nuc + 2)[1:-1],
            asarray([200 for i in range(self.n_nuc)]),
            asarray([0.9 / self.n_nuc for i in range(self.n_nuc)]),
        ]
        self.rp = None
        self.nucfree_dist_family=nucfree_dist_family

    def total_pmf(self, para):
        alpha, mu, mus, sigmas, ps = para
        x = asarray(self.keys)
        p = 1 - sum(ps)
        ps = asarray([[p] + list(ps)]).T
        y = [
            stats.gamma(alpha, scale=mu).cdf(x + 0.5)
            - stats.gamma(alpha, scale=mu).cdf(x - 0.5)
        ]
        for a, b in zip(mus, sigmas):
            y.append(
                stats.norm(loc=a, scale=b).cdf(x + 0.5)
                - stats.norm(loc=a, scale=b).cdf(x - 0.5)
            )
        y = asarray(y).T.dot(ps)
        if any(isnan(y)):
            raise ValueError
        return y.T[0]

    def e_step(self, para):
        alpha, mu, mus, sigmas, ps = para
        x = asarray(self.keys)
        t = self.total_pmf(para)
        y = [
            (1 - sum(ps))
            * (
                stats.gamma(alpha, scale=mu).cdf(x + 0.5)
                - stats.gamma(alpha, scale=mu).cdf(x - 0.5)
            )
            / t
        ]
        for a, b, c in zip(mus, sigmas, ps):
            y.append(
                (
                    stats.norm(loc=a, scale=b).cdf(x + 0.5)
                    - stats.norm(loc=a, scale=b).cdf(x - 0.5)
                )
                * c
                / t
            )
        return asarray(y).T

    def m_step(self, gm):
        x = asarray(self.keys)
        freq = asarray(self.values)
        gm = gm.T
        mu = sum(x * freq * gm[0]) / sum(freq * gm[0])
        xb = log(mu)
        bx = sum(log(x) * freq * gm[0]) / sum(freq * gm[0])
        if self.nucfree_dist_family=='gamma':
            alpha = self.estimate_alpha(bx, xb)
        else:
            alpha = 1.0
        mu = mu / alpha
        mus = [sum(x * freq * k) / sum(freq * k) for k in gm[1:]]
        sigmas = [
            sum(((x - m) ** 2) * freq * k) / sum(freq * k) for m, k in zip(mus, gm[1:])
        ]
        sigmas = [sqrt(i) for i in sigmas]
        ps = [sum(k * freq) / sum(freq) for k in gm[1:]]
        return alpha, mu, mus, sigmas, ps

    def estimate_alpha(self, bx, xb):
        initValue = 0.5 / (xb - bx)

        def optFun(a, bx, xb):
            newa = 1 / a + (bx - xb + log(a) - special.digamma(a)) / (
                a**2 * (1 / a - special.polygamma(1, a))
            )
            newa = 1 / newa
            if abs(a - newa) < 0.0001:
                return newa
            else:
                return optFun(newa, bx, xb)

        return optFun(initValue, bx, xb)

    def __call__(self):
        params = [
            [
                0,
                0,
                zeros_like(self.params[2]),
                zeros_like(self.params[3]),
                ones_like(self.params[4]),
            ],
            self.params,
        ]
        ix = 0
        early_stop = -1
        try:
            while (
                mean(
                    abs(asarray(params[0][-1]) - asarray(params[1][-1]))
                    / asarray(params[0][-1])
                )
                > 0.0001
            ):
                try:
                    gm = self.e_step(params[-1])
                except ValueError:
                    print(
                        "Model with %d nucleosomes early stop because overfitting."
                        % self.n_nuc
                    )
                    early_stop = -2
                    break
                if any(isnan(gm)):
                    print(
                        "Model with %d nucleosomes early stop because lack of fit."
                        % self.n_nuc
                    )
                    early_stop = -2
                    break
                params.append(self.m_step(gm))
                del params[0]
                ix += 1
                assert ix <= 10000
            self.params = params[early_stop]
            self.rp = self.e_step(self.params)
            return self
        except AssertionError as ex:
            print("EM algorithm does not converge!")
            raise ex

    def AIC(self):
        N = float(sum(self.fl))
        d = len(self.params[-1]) * 3 + 2
        loglik = sum(log(self.total_pmf(self.params)) * self.nums)
        return -2.0 / N * loglik + 2.0 * d / N

    @property
    def minLength(self):
        x = self.keys[where(self.rp[:, 1] >= 0.5)[0]] - 0.5 + self.lengthShift
        return int(min(x))

    @property
    def maxLength(self):
        x = self.keys[where(self.rp[:, 1] >= 0.5)[0]] - 0.5 + self.lengthShift
        return int(max(x))

    @property
    def which_is_long(self):
        x = self.keys[where(self.rp[:, 0] > 0.5)[0]] - 0.5 + self.lengthShift
        return int(max(x))

    def neededToBeAdded(self, step=5):
        y = {}
        for l, v in zip(self.keys, self.rp):
            l = int(l - 0.5 + self.lengthShift)
            y[l] = {}
            for k in range(1, len(v)):
                h = l / float(k)
                for i in (0.5 * h + h * arange(k)).astype(int):
                    for j in range(i - step, i + step + 1):
                        if 0 <= j < l:
                            y[l][j] = y[l].get(j, 0) + v[k]
        y = {k: asarray(sorted(v.items(), key=lambda x: x[0])) for k, v in y.items()}
        y = {k: (v[:, 0].astype(int), v[:, 1].astype(float)) for k, v in y.items()}
        return y

    def nucFreeTrack(
        self,
        samFiles,
        outputFile,
        smoothFile,
        chromSkip="",
        chrom_inculde="",
        leftShift=+4,
        rightShift=-5,
    ):
        nucFree = dict(
            zip(asarray(self.fl.index + self.lengthShift, dtype=int), self.rp[:, 0])
        )
        with pysam.AlignmentFile(samFiles[0]) as sam:
            freeTracks = {
                k: zeros(v)
                for k, v in zip(sam.references, sam.lengths)
                if (not k in chromSkip) and (re.sub(chrom_inculde, "", k) == "")
            }
        for samFile in samFiles:
            with pysam.AlignmentFile(samFile) as sam:
                rds = {}
                ix = 0
                for r2 in sam.fetch(until_eof=True):
                    try:
                        if r2.reference_name in freeTracks:
                            r1 = rds.pop(r2.query_name)
                            p1 = min(r1.reference_start, r2.reference_start) + leftShift
                            p2 = max(r1.reference_end, r2.reference_end) + rightShift
                            freeTracks[r1.reference_name][p1:p2] += nucFree[p2 - p1]
                            ix += 1
                    except KeyError:
                        rds[r2.query_name] = r2
        with h5py.File(outputFile, "a") as hdf:
            hdf.create_group("short")
            for k, v in freeTracks.items():
                hdf.create_dataset("short/%s" % k, data=v)
        proc = signal_track_builder.GaussConvolve(
            outputFile, smoothFile, "short", 72, third_dev=False
        )
        proc.start()
        proc.join()


def fragmentLengthModel(fl,nuc_number=0,nucfree_dist_family='gamma'):
    if nuc_number==0:
        candModel = []
        for i in range(3, 10):
            try:
                m = EMSmoothFragLenDist(fl, i,nucfree_dist_family)()
                candModel.append(m)
            except Exception as ex:
                pass
        return max(candModel, key=lambda k: k.AIC())
    else:
        m = EMSmoothFragLenDist(fl, nuc_number,nucfree_dist_family)()
        return m

def fix_parameters(parameters):
    import numpy as np
    fixed_parameters=[]
    for idx in range(2):
        fixed_parameters.append(parameters[idx])
    order = np.argsort(parameters[2])
    for idx in range(2,5):
        fixed_parameters.append(list(np.array(parameters[idx])[list(order)]))
    return tuple(fixed_parameters)
