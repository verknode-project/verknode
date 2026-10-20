"""rdkit.SimDivFilters.rdSimDivPickers in the browser version: MaxMin diversity picking on Tanimoto
distances (the same algorithm as RDKit's; with a seed the first pick is random, so picks may differ from
the server's)."""
import random

from .._core import tanimoto


class MaxMinPicker:
    def LazyBitVectorPick(self, fps, poolSize: int, pickSize: int, firstPicks=(), seed: int = -1, **_):
        n, k = min(int(poolSize), len(fps)), int(pickSize)
        if k > n:
            raise ValueError("pickSize > poolSize")
        picks = list(firstPicks)
        if not picks and k:
            picks = [random.Random(seed).randrange(n) if seed >= 0 else 0]
        mind = [float("inf")] * n
        for p in picks:
            for i in range(n):
                mind[i] = min(mind[i], 1.0 - tanimoto(fps[p], fps[i]))
        while len(picks) < k:
            best = max((i for i in range(n) if i not in picks), key=lambda i: mind[i])
            picks.append(best)
            for i in range(n):
                mind[i] = min(mind[i], 1.0 - tanimoto(fps[best], fps[i]))
        return list(picks)

    LazyPick = None
