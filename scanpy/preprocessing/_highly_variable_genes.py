from __future__ import annotations

import warnings
from dataclasses import dataclass
from functools import partial
from inspect import signature
from typing import TYPE_CHECKING, Literal, cast

import numpy as np
import pandas as pd
import scipy.sparse as sp_sparse
from anndata import AnnData

from .. import logging as logg
from .._compat import (
    DaskArray,
    DaskDataFrame,
    DaskDataFrameGroupBy,
    DaskSeries,
    DaskSeriesGroupBy,
    old_positionals,
)
from .._settings import Verbosity, settings
from .._utils import check_nonnegative_integers, sanitize_anndata
from ..get import _get_obs_rep
from ._distributed import (
    dask_compute,
    get_mad,
    materialize_as_ndarray,
    series_to_array,
    suppress_pandas_warning,
)
from ._simple import filter_genes
from ._utils import _get_mean_var

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from pandas.core.groupby.generic import DataFrameGroupBy, SeriesGroupBy


def _highly_variable_genes_seurat_v3(
    adata: AnnData,
    *,
    layer: str | None = None,
    n_top_genes: int = 2000,
    batch_key: str | None = None,
    check_values: bool = True,
    span: float = 0.3,
    subset: bool = False,
    inplace: bool = True,
) -> pd.DataFrame | None:
    """\
    See `highly_variable_genes`.

    For further implementation details see https://www.overleaf.com/read/ckptrbgzzzpg

    Returns
    -------
    Depending on `inplace` returns calculated metrics (:class:`~pd.DataFrame`) or
    updates `.var` with the following fields:

    highly_variable : :class:`bool`
        boolean indicator of highly-variable genes.
    **means**
        means per gene.
    **variances**
        variance per gene.
    **variances_norm**
        normalized variance per gene, averaged in the case of multiple batches.
    highly_variable_rank : :class:`float`
        Rank of the gene according to normalized variance, median rank in the case of multiple batches.
    highly_variable_nbatches : :class:`int`
        If batch_key is given, this denotes in how many batches genes are detected as HVG.
    """

    try:
        from skmisc.loess import loess
    except ImportError:
        raise ImportError(
            "Please install skmisc package via `pip install --user scikit-misc"
        )
    df = pd.DataFrame(index=adata.var_names)
    data = _get_obs_rep(adata, layer=layer)

    if check_values and not check_nonnegative_integers(data):
        warnings.warn(
            "`flavor='seurat_v3'` expects raw count data, but non-integers were found.",
            UserWarning,
        )

    df["means"], df["variances"] = _get_mean_var(data)

    if batch_key is None:
        batch_info = pd.Categorical(np.zeros(adata.shape[0], dtype=int))
    else:
        batch_info = adata.obs[batch_key].to_numpy()

    norm_gene_vars = []
    for b in np.unique(batch_info):
        data_batch = data[batch_info == b]

        mean, var = _get_mean_var(data_batch)
        not_const = var > 0
        estimat_var = np.zeros(data.shape[1], dtype=np.float64)

        y = np.log10(var[not_const])
        x = np.log10(mean[not_const])
        model = loess(x, y, span=span, degree=2)
        model.fit()
        estimat_var[not_const] = model.outputs.fitted_values
        reg_std = np.sqrt(10**estimat_var)

        batch_counts = data_batch.astype(np.float64).copy()
        # clip large values as in Seurat
        N = data_batch.shape[0]
        vmax = np.sqrt(N)
        clip_val = reg_std * vmax + mean
        if sp_sparse.issparse(batch_counts):
            batch_counts = sp_sparse.csr_matrix(batch_counts)
            mask = batch_counts.data > clip_val[batch_counts.indices]
            batch_counts.data[mask] = clip_val[batch_counts.indices[mask]]

            squared_batch_counts_sum = np.array(batch_counts.power(2).sum(axis=0))
            batch_counts_sum = np.array(batch_counts.sum(axis=0))
        else:
            clip_val_broad = np.broadcast_to(clip_val, batch_counts.shape)
            np.putmask(
                batch_counts,
                batch_counts > clip_val_broad,
                clip_val_broad,
            )

            squared_batch_counts_sum = np.square(batch_counts).sum(axis=0)
            batch_counts_sum = batch_counts.sum(axis=0)

        norm_gene_var = (1 / ((N - 1) * np.square(reg_std))) * (
            (N * np.square(mean))
            + squared_batch_counts_sum
            - 2 * batch_counts_sum * mean
        )
        norm_gene_vars.append(norm_gene_var.reshape(1, -1))

    norm_gene_vars = np.concatenate(norm_gene_vars, axis=0)
    # argsort twice gives ranks, small rank means most variable
    ranked_norm_gene_vars = np.argsort(np.argsort(-norm_gene_vars, axis=1), axis=1)

    # this is done in SelectIntegrationFeatures() in Seurat v3
    ranked_norm_gene_vars = ranked_norm_gene_vars.astype(np.float32)
    num_batches_high_var = np.sum(
        (ranked_norm_gene_vars < n_top_genes).astype(int), axis=0
    )
    ranked_norm_gene_vars[ranked_norm_gene_vars >= n_top_genes] = np.nan
    ma_ranked = np.ma.masked_invalid(ranked_norm_gene_vars)
    median_ranked = np.ma.median(ma_ranked, axis=0).filled(np.nan)

    df["highly_variable_nbatches"] = num_batches_high_var
    df["highly_variable_rank"] = median_ranked
    df["variances_norm"] = np.mean(norm_gene_vars, axis=0)

    sorted_index = (
        df[["highly_variable_rank", "highly_variable_nbatches"]]
        .sort_values(
            ["highly_variable_rank", "highly_variable_nbatches"],
            ascending=[True, False],
            na_position="last",
        )
        .index
    )
    df["highly_variable"] = False
    df.loc[sorted_index[: int(n_top_genes)], "highly_variable"] = True

    if inplace:
        adata.uns["hvg"] = {"flavor": "seurat_v3"}
        logg.hint(
            "added\n"
            "    'highly_variable', boolean vector (adata.var)\n"
            "    'highly_variable_rank', float vector (adata.var)\n"
            "    'means', float vector (adata.var)\n"
            "    'variances', float vector (adata.var)\n"
            "    'variances_norm', float vector (adata.var)"
        )
        adata.var["highly_variable"] = df["highly_variable"].to_numpy()
        adata.var["highly_variable_rank"] = df["highly_variable_rank"].to_numpy()
        adata.var["means"] = df["means"].to_numpy()
        adata.var["variances"] = df["variances"].to_numpy()
        adata.var["variances_norm"] = (
            df["variances_norm"].to_numpy().astype("float64", copy=False)
        )
        if batch_key is not None:
            adata.var["highly_variable_nbatches"] = df[
                "highly_variable_nbatches"
            ].to_numpy()
        if subset:
            adata._inplace_subset_var(df["highly_variable"].to_numpy())
    else:
        if batch_key is None:
            df = df.drop(["highly_variable_nbatches"], axis=1)
        if subset:
            df = df.iloc[df["highly_variable"].to_numpy(), :]

        return df


@dataclass
class _Cutoffs:
    min_disp: float
    max_disp: float
    min_mean: float
    max_mean: float

    @classmethod
    def validate(
        cls,
        *,
        n_top_genes: int | None,
        min_disp: float,
        max_disp: float,
        min_mean: float,
        max_mean: float,
    ) -> _Cutoffs | int:
        if n_top_genes is None:
            return cls(min_disp, max_disp, min_mean, max_mean)

        cutoffs = {"min_disp", "max_disp", "min_mean", "max_mean"}
        defaults = {
            p.name: p.default
            for p in signature(highly_variable_genes).parameters.values()
            if p.name in cutoffs
        }
        if {k: v for k, v in locals().items() if k in cutoffs} != defaults:
            logg.info("If you pass `n_top_genes`, all cutoffs are ignored.")
        return n_top_genes

    def in_bounds(
        self,
        mean: NDArray[np.float64] | DaskArray,
        dispersion_norm: NDArray[np.float64] | DaskArray,
    ) -> NDArray[np.bool_] | DaskArray:
        return (
            (mean > self.min_mean)
            & (mean < self.max_mean)
            & (dispersion_norm > self.min_disp)
            & (dispersion_norm < self.max_disp)
        )


def _highly_variable_genes_single_batch(
    adata: AnnData,
    *,
    layer: str | None = None,
    cutoff: _Cutoffs | int,
    n_bins: int = 20,
    flavor: Literal["seurat", "cell_ranger"] = "seurat",
) -> pd.DataFrame | DaskDataFrame:
    """\
    See `highly_variable_genes`.

    Returns
    -------
    A DataFrame that contains the columns
    `highly_variable`, `means`, `dispersions`, and `dispersions_norm`.
    """
    data = _get_obs_rep(adata, layer=layer)
    if flavor == "seurat":
        data = data.copy()
        if "log1p" in adata.uns_keys() and adata.uns["log1p"].get("base") is not None:
            data *= np.log(adata.uns["log1p"]["base"])
        # use out if possible. only possible since we copy the data matrix
        if isinstance(data, np.ndarray):
            np.expm1(data, out=data)
        else:
            data = np.expm1(data)

    mean, var = _get_mean_var(data)
    # now actually compute the dispersion
    mean[mean == 0] = 1e-12  # set entries equal to zero to small value
    dispersion = var / mean
    if flavor == "seurat":  # logarithmized mean as in Seurat
        dispersion[dispersion == 0] = np.nan
        dispersion = np.log(dispersion)
        mean = np.log1p(mean)

    # all of the following quantities are "per-gene" here
    df: pd.DataFrame | DaskDataFrame
    if isinstance(data, DaskArray):
        import dask.array as da
        import dask.dataframe as dd

        df = dd.from_dask_array(
            da.vstack((mean, dispersion)).T,
            columns=["means", "dispersions"],
        )
        df["gene"] = adata.var_names.to_series(index=df.index, name="gene")
        df = df.set_index("gene")
    else:
        df = pd.DataFrame(
            dict(means=mean, dispersions=dispersion), index=adata.var_names
        )
    df.index.name = "gene"
    df["mean_bin"] = _get_mean_bins(df["means"], flavor, n_bins)
    disp_grouped = df.groupby("mean_bin", observed=True)["dispersions"]
    if flavor == "seurat":
        disp_stats = _stats_seurat(df["mean_bin"], disp_grouped)
    elif flavor == "cell_ranger":
        disp_stats = _stats_cell_ranger(df["mean_bin"], disp_grouped)
    else:
        raise ValueError('`flavor` needs to be "seurat" or "cell_ranger"')

    # actually do the normalization
    df["dispersions_norm"] = (df["dispersions"] - disp_stats["avg"]) / disp_stats["dev"]
    df["highly_variable"] = _subset_genes(
        adata,
        mean=mean,
        dispersion_norm=series_to_array(df["dispersions_norm"]),
        cutoff=cutoff,
    )

    return df


def _get_mean_bins(
    means: pd.Series | DaskSeries, flavor: Literal["seurat", "cell_ranger"], n_bins: int
) -> pd.Series | DaskSeries:
    if flavor == "seurat":
        bins = n_bins
    elif flavor == "cell_ranger":
        bins = np.r_[-np.inf, np.percentile(means, np.arange(10, 105, 5)), np.inf]
    else:
        raise ValueError('`flavor` needs to be "seurat" or "cell_ranger"')

    if isinstance(means, DaskSeries):
        # TODO: does map_partitions make sense for bin? It would bin per chunk, not globally
        return means.map_partitions(pd.cut, bins=bins)
    return pd.cut(means, bins=bins)


def _stats_seurat(
    mean_bins: pd.Series | DaskSeries,
    disp_grouped: SeriesGroupBy | DaskSeriesGroupBy,
) -> pd.DataFrame | DaskDataFrame:
    """Compute mean and std dev per bin."""
    with suppress_pandas_warning():
        disp_bin_stats: pd.DataFrame = dask_compute(
            disp_grouped.agg(avg="mean", dev=partial(np.std, ddof=1))
        )
    # retrieve those genes that have nan std, these are the ones where
    # only a single gene fell in the bin and implicitly set them to have
    # a normalized disperion of 1
    one_gene_per_bin = disp_bin_stats["dev"].isnull()
    gen_indices = np.flatnonzero(one_gene_per_bin.loc[mean_bins])
    if len(gen_indices) > 0:
        logg.debug(
            f"Gene indices {gen_indices} fell into a single bin: their "
            "normalized dispersion was set to 1.\n    "
            "Decreasing `n_bins` will likely avoid this effect."
        )
        disp_bin_stats["dev"].loc[one_gene_per_bin] = disp_bin_stats["avg"].loc[
            one_gene_per_bin
        ]
        disp_bin_stats["avg"].loc[one_gene_per_bin] = 0
    return _unbin(disp_bin_stats, mean_bins)


def _stats_cell_ranger(
    mean_bins: pd.Series | DaskSeries,
    disp_grouped: SeriesGroupBy | DaskSeriesGroupBy,
) -> pd.DataFrame | DaskDataFrame:
    """Compute median and median absolute dev per bin."""

    is_dask = isinstance(disp_grouped, DaskSeriesGroupBy)
    with warnings.catch_warnings():
        # MAD calculation raises the warning: "Mean of empty slice"
        warnings.simplefilter("ignore", category=RuntimeWarning)
        disp_bin_stats = _aggregate(disp_grouped, ["median", get_mad(dask=is_dask)])
    # Can’t use kwargs in `aggregate`: https://github.com/dask/dask/issues/10836
    disp_bin_stats = disp_bin_stats.rename(columns=dict(median="avg", mad="dev"))
    return _unbin(disp_bin_stats, mean_bins)


def _unbin(
    df: pd.DataFrame | DaskDataFrame, mean_bins: pd.Series | DaskSeries
) -> pd.DataFrame | DaskDataFrame:
    df = df.loc[mean_bins]
    df["gene"] = mean_bins.index
    return df.set_index("gene")


def _aggregate(
    grouped: (
        DataFrameGroupBy | DaskDataFrameGroupBy | SeriesGroupBy | DaskSeriesGroupBy
    ),
    arg=None,
    **kw,
) -> pd.DataFrame | DaskDataFrame | pd.Series | DaskSeries:
    # ValueError: In order to aggregate with 'median',
    # you must use shuffling-based aggregation (e.g., shuffle='tasks')
    if ((arg and "median" in arg) or "median" in kw) and isinstance(
        grouped, (DaskSeriesGroupBy, DaskDataFrameGroupBy)
    ):
        kw["shuffle"] = True
    return grouped.agg(arg, **kw)


def _subset_genes(
    adata: AnnData,
    *,
    mean: NDArray[np.float64] | DaskArray,
    dispersion_norm: NDArray[np.float64] | DaskArray,
    cutoff: _Cutoffs | int,
) -> NDArray[np.bool_] | DaskArray:
    """Get boolean mask of genes with normalized dispersion in bounds."""
    if isinstance(cutoff, _Cutoffs):
        dispersion_norm[np.isnan(dispersion_norm)] = 0  # similar to Seurat
        return cutoff.in_bounds(mean, dispersion_norm)
    n_top_genes = cutoff
    del cutoff

    if n_top_genes > adata.n_vars:
        logg.info("`n_top_genes` > `adata.n_var`, returning all genes.")
        n_top_genes = adata.n_vars
    disp_cut_off = _nth_highest(dispersion_norm, n_top_genes)
    logg.debug(
        f"the {n_top_genes} top genes correspond to a "
        f"normalized dispersion cutoff of {disp_cut_off}"
    )
    return np.nan_to_num(dispersion_norm) >= disp_cut_off


def _nth_highest(x: NDArray[np.float64] | DaskArray, n: int) -> float | DaskArray:
    x = x[~np.isnan(x)]
    if n > x.size:
        msg = "`n_top_genes` > number of normalized dispersions, returning all genes with normalized dispersions."
        warnings.warn(msg, UserWarning)
        n = x.size
    if isinstance(x, DaskArray):
        return x.topk(n)[-1]
    # interestingly, np.argpartition is slightly slower
    x[::-1].sort()
    return x[n - 1]


def _highly_variable_genes_batched(
    adata: AnnData,
    batch_key: str,
    *,
    layer: str | None,
    n_bins: int,
    flavor: Literal["seurat", "cell_ranger"],
    cutoff: _Cutoffs | int,
) -> pd.DataFrame | DaskDataFrame:
    sanitize_anndata(adata)
    batches = adata.obs[batch_key].cat.categories
    dfs = []
    gene_list = adata.var_names
    for batch in batches:
        adata_subset = adata[adata.obs[batch_key] == batch]

        # Filter to genes that are in the dataset
        with settings.verbosity.override(Verbosity.error):
            # TODO use groupby or so instead of materialize_as_ndarray
            filt, _ = materialize_as_ndarray(
                filter_genes(
                    _get_obs_rep(adata_subset, layer=layer),
                    min_cells=1,
                    inplace=False,
                )
            )

        adata_subset = adata_subset[:, filt]

        hvg = _highly_variable_genes_single_batch(
            adata_subset, layer=layer, cutoff=cutoff, n_bins=n_bins, flavor=flavor
        )
        assert hvg.index.name == "gene"
        if isinstance(hvg, DaskDataFrame):
            hvg = hvg.reset_index(drop=False)
        else:
            hvg.reset_index(drop=False, inplace=True)

        if (n_removed := np.sum(~filt)) > 0:
            # Add 0 values for genes that were filtered out
            missing_hvg = pd.DataFrame(
                np.zeros((n_removed, len(hvg.columns))),
                columns=hvg.columns,
            )
            missing_hvg["highly_variable"] = missing_hvg["highly_variable"].astype(bool)
            missing_hvg["gene"] = gene_list[~filt]
            hvg = pd.concat([hvg, missing_hvg], ignore_index=True)

        dfs.append(hvg)

    df: DaskDataFrame | pd.DataFrame
    if isinstance(dfs[0], DaskDataFrame):
        import dask.dataframe as dd

        df = dd.concat(dfs, axis=0)
    else:
        df = pd.concat(dfs, axis=0)

    df["highly_variable"] = df["highly_variable"].astype(int)
    df = df.groupby("gene", observed=True).agg(
        dict(
            means="mean",
            dispersions="mean",
            dispersions_norm="mean",
            highly_variable="sum",
        )
    )
    if isinstance(df, DaskDataFrame):
        df = df.set_index("gene")  # happens automatically for pandas df
    df["highly_variable_nbatches"] = df["highly_variable"]
    df["highly_variable_intersection"] = df["highly_variable_nbatches"] == len(batches)

    if isinstance(cutoff, int):
        # sort genes by how often they selected as hvg within each batch and
        # break ties with normalized dispersion across batches
        df.sort_values(
            ["highly_variable_nbatches", "dispersions_norm"],
            ascending=False,
            na_position="last",
            inplace=True,
        )
        df["highly_variable"] = np.arange(df.shape[0]) < cutoff
    else:
        dispersion_norm = series_to_array(df["dispersions_norm"])
        dispersion_norm[np.isnan(dispersion_norm)] = 0  # similar to Seurat
        df["highly_variable"] = cutoff.in_bounds(df["means"], df["dispersions_norm"])

    return df


@old_positionals(
    "layer",
    "n_top_genes",
    "min_disp",
    "max_disp",
    "min_mean",
    "max_mean",
    "span",
    "n_bins",
    "flavor",
    "subset",
    "inplace",
    "batch_key",
    "check_values",
)
def highly_variable_genes(
    adata: AnnData,
    *,
    layer: str | None = None,
    n_top_genes: int | None = None,
    min_disp: float = 0.5,
    max_disp: float = np.inf,
    min_mean: float = 0.0125,
    max_mean: float = 3,
    span: float = 0.3,
    n_bins: int = 20,
    flavor: Literal["seurat", "cell_ranger", "seurat_v3"] = "seurat",
    subset: bool = False,
    inplace: bool = True,
    batch_key: str | None = None,
    check_values: bool = True,
) -> pd.DataFrame | DaskDataFrame | None:
    """\
    Annotate highly variable genes [Satija15]_ [Zheng17]_ [Stuart19]_.

    Expects logarithmized data, except when `flavor='seurat_v3'`, in which count
    data is expected.

    Depending on `flavor`, this reproduces the R-implementations of Seurat
    [Satija15]_, Cell Ranger [Zheng17]_, and Seurat v3 [Stuart19]_. Seurat v3 flavor
    requires `scikit-misc` package. If you plan to use this flavor, consider
    installing `scanpy` with this optional dependency: `scanpy[skmisc]`.

    For the dispersion-based methods (`flavor='seurat'` [Satija15]_ and
    `flavor='cell_ranger'` [Zheng17]_), the normalized dispersion is obtained
    by scaling with the mean and standard deviation of the dispersions for genes
    falling into a given bin for mean expression of genes. This means that for each
    bin of mean expression, highly variable genes are selected.

    For `flavor='seurat_v3'` [Stuart19]_, a normalized variance for each gene
    is computed. First, the data are standardized (i.e., z-score normalization
    per feature) with a regularized standard deviation. Next, the normalized variance
    is computed as the variance of each gene after the transformation. Genes are ranked
    by the normalized variance.

    See also `scanpy.experimental.pp._highly_variable_genes` for additional flavours
    (e.g. Pearson residuals).

    Parameters
    ----------
    adata
        The annotated data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    layer
        If provided, use `adata.layers[layer]` for expression values instead of `adata.X`.
    n_top_genes
        Number of highly-variable genes to keep. Mandatory if `flavor='seurat_v3'`.
    min_mean
        If `n_top_genes` unequals `None`, this and all other cutoffs for the means and the
        normalized dispersions are ignored. Ignored if `flavor='seurat_v3'`.
    max_mean
        If `n_top_genes` unequals `None`, this and all other cutoffs for the means and the
        normalized dispersions are ignored. Ignored if `flavor='seurat_v3'`.
    min_disp
        If `n_top_genes` unequals `None`, this and all other cutoffs for the means and the
        normalized dispersions are ignored. Ignored if `flavor='seurat_v3'`.
    max_disp
        If `n_top_genes` unequals `None`, this and all other cutoffs for the means and the
        normalized dispersions are ignored. Ignored if `flavor='seurat_v3'`.
    span
        The fraction of the data (cells) used when estimating the variance in the loess
        model fit if `flavor='seurat_v3'`.
    n_bins
        Number of bins for binning the mean gene expression. Normalization is
        done with respect to each bin. If just a single gene falls into a bin,
        the normalized dispersion is artificially set to 1. You'll be informed
        about this if you set `settings.verbosity = 4`.
    flavor
        Choose the flavor for identifying highly variable genes. For the dispersion
        based methods in their default workflows, Seurat passes the cutoffs whereas
        Cell Ranger passes `n_top_genes`.
    subset
        Inplace subset to highly-variable genes if `True` otherwise merely indicate
        highly variable genes.
    inplace
        Whether to place calculated metrics in `.var` or return them.
    batch_key
        If specified, highly-variable genes are selected within each batch separately and merged.
        This simple process avoids the selection of batch-specific genes and acts as a
        lightweight batch correction method. For all flavors, genes are first sorted
        by how many batches they are a HVG. For dispersion-based flavors ties are broken
        by normalized dispersion. If `flavor = 'seurat_v3'`, ties are broken by the median
        (across batches) rank based on within-batch normalized variance.
    check_values
        Check if counts in selected layer are integers. A Warning is returned if set to True.
        Only used if `flavor='seurat_v3'`.

    Returns
    -------
    Returns a :class:`pandas.DataFrame` with calculated metrics if `inplace=True`, else returns an `AnnData` object where it sets the following field:

    `adata.var['highly_variable']` : :class:`pandas.Series` (dtype `bool`)
        boolean indicator of highly-variable genes
    `adata.var['means']` : :class:`pandas.Series` (dtype `float`)
        means per gene
    `adata.var['dispersions']` : :class:`pandas.Series` (dtype `float`)
        For dispersion-based flavors, dispersions per gene
    `adata.var['dispersions_norm']` : :class:`pandas.Series` (dtype `float`)
        For dispersion-based flavors, normalized dispersions per gene
    `adata.var['variances']` : :class:`pandas.Series` (dtype `float`)
        For `flavor='seurat_v3'`, variance per gene
    `adata.var['variances_norm']` : :class:`pandas.Series` (dtype `float`)
        For `flavor='seurat_v3'`, normalized variance per gene, averaged in
        the case of multiple batches
    `adata.var['highly_variable_rank']` : :class:`pandas.Series` (dtype `float`)
        For `flavor='seurat_v3'`, rank of the gene according to normalized
        variance, median rank in the case of multiple batches
    `adata.var['highly_variable_nbatches']` : :class:`pandas.Series` (dtype `int`)
        If `batch_key` is given, this denotes in how many batches genes are detected as HVG
    `adata.var['highly_variable_intersection']` : :class:`pandas.Series` (dtype `bool`)
        If `batch_key` is given, this denotes the genes that are highly variable in all batches

    Notes
    -----
    This function replaces :func:`~scanpy.pp.filter_genes_dispersion`.
    """

    start = logg.info("extracting highly variable genes")

    if not isinstance(adata, AnnData):
        raise ValueError(
            "`pp.highly_variable_genes` expects an `AnnData` argument, "
            "pass `inplace=False` if you want to return a `pd.DataFrame`."
        )

    if flavor == "seurat_v3":
        if n_top_genes is None:
            sig = signature(_highly_variable_genes_seurat_v3)
            n_top_genes = cast(int, sig.parameters["n_top_genes"].default)
        return _highly_variable_genes_seurat_v3(
            adata,
            layer=layer,
            n_top_genes=n_top_genes,
            batch_key=batch_key,
            check_values=check_values,
            span=span,
            subset=subset,
            inplace=inplace,
        )

    cutoff = _Cutoffs.validate(
        n_top_genes=n_top_genes,
        min_disp=min_disp,
        max_disp=max_disp,
        min_mean=min_mean,
        max_mean=max_mean,
    )
    del min_disp, max_disp, min_mean, max_mean, n_top_genes

    if batch_key is None:
        df = _highly_variable_genes_single_batch(
            adata, layer=layer, cutoff=cutoff, n_bins=n_bins, flavor=flavor
        )
    else:
        df = _highly_variable_genes_batched(
            adata, batch_key, layer=layer, cutoff=cutoff, n_bins=n_bins, flavor=flavor
        )

    logg.info("    finished", time=start)

    if inplace:
        adata.uns["hvg"] = {"flavor": flavor}
        logg.hint(
            "added\n"
            "    'highly_variable', boolean vector (adata.var)\n"
            "    'means', float vector (adata.var)\n"
            "    'dispersions', float vector (adata.var)\n"
            "    'dispersions_norm', float vector (adata.var)"
        )
        adata.var["highly_variable"] = dask_compute(df["highly_variable"])
        adata.var["means"] = dask_compute(df["means"])
        adata.var["dispersions"] = dask_compute(df["dispersions"])
        adata.var["dispersions_norm"] = dask_compute(df["dispersions_norm"]).astype(
            np.float32, copy=False
        )

        if batch_key is not None:
            adata.var["highly_variable_nbatches"] = dask_compute(
                df["highly_variable_nbatches"]
            )
            adata.var["highly_variable_intersection"] = dask_compute(
                df["highly_variable_intersection"]
            )
        if subset:
            adata._inplace_subset_var(materialize_as_ndarray(df["highly_variable"]))

    else:
        if subset:
            df = df.loc[df["highly_variable"]]

        return df
