"""regional_check: the verknode workflow regional_check.vkf as a standalone script - it runs without Jupyter
(a terminal, cron, CI, a container, another program).

    python regional_check.py                            # run it, print the results
    python regional_check.py --var threshold=200.0      # with another workflow variable
    python regional_check.py --out results/             # and write the result tables (--format parquet, json)
    python regional_check.py --save regional_check.vkf  # and write the workflow, to open it in the editor

File paths are relative to --base-dir (default: this script's folder). It needs the
jupyterlab-verknode package (and the packages its nodes use) - not a Jupyter server.
Exported by verknode on 2026-10-18.
"""
import os

from jupyterlab_verknode.standalone import cli

VARIABLES = {'threshold': 200.0}  # workflow variables (--var NAME=VALUE)


def build(wf):
    """The nodes and their connections; returns the result nodes."""
    sales = wf.add_node('CSV Reader', 'Sales', path='../agent/sales.csv', at=(0, 200))
    revenue = wf.add_node(
        'Math Formula',
        'Revenue',
        expression='$amount$ * $quantity$',
        new_column='revenue',
        at=(150, 200),
    )
    revenue_per_region = wf.add_node(
        'GroupBy',
        'Revenue per region',
        groups={'include': ['region'], 'exclude': [], 'enforce': 'inclusion'},
        aggregations=[{'column': 'revenue', 'method': 'Sum', 'name': 'revenue'}],
        at=(300, 200),
    )
    targets = wf.add_node('CSV Reader', 'Targets', path='../agent/regions.csv', at=(450, 200))
    with_targets = wf.add_node(
        'Joiner',
        'With targets',
        keys=[{'left': 'region', 'right': 'region'}],
        at=(450, 320),
    )
    of_target = wf.add_node(
        'Math Formula',
        '% of target',
        expression='round($revenue$ / $target$ * 100, 1)',
        new_column='pct_of_target',
        at=(600, 320),
    )
    below_threshold = wf.add_node(
        'Row Filter',
        'Below threshold',
        column='pct_of_target',
        operator='<',
        at=(750, 320),
    )
    wf.bind(below_threshold, 'value', 'threshold')

    wf.connect(sales, revenue)
    wf.connect(revenue, revenue_per_region)
    wf.connect(revenue_per_region, with_targets)
    wf.connect(targets, with_targets, dst_port=2)
    wf.connect(with_targets, of_target)
    wf.connect(of_target, below_threshold)

    return {'Below threshold': below_threshold}


if __name__ == "__main__":
    cli(build, VARIABLES, base_dir=os.path.dirname(os.path.abspath(__file__)), description=__doc__)
