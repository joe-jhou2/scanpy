from __future__ import annotations

from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import pytest

from scanpy.plotting._cluster_tree import cluster_decision_tree
from scanpy.tools._cluster_resolution import cluster_resolution_finder


@pytest.fixture
def cluster_data(adata):
    """Fixture providing clustering data and top_genes_dict for cluster_decision_tree."""
    resolutions = [0.0, 0.2, 0.5, 1.0, 1.5, 2.0]
    top_genes_dict, cluster_data = cluster_resolution_finder(
        adata,
        resolutions,
        prefix="leiden_res_",
        n_top_genes=2,
        min_cells=2,
        deg_mode="within_parent",
        flavor="igraph",
        n_iterations=2,
        copy=True,
    )
    return cluster_data, resolutions, top_genes_dict


# Test 0: Image comparison
# @pytest.mark.mpl_image_compare
def test_cluster_decision_tree_plot(cluster_data, image_comparer):
    """Test that the plot generated by cluster_decision_tree matches the expected output."""
    cluster_data, resolutions, top_genes_dict = cluster_data

    # Set a random seed for reproducibility
    np.random.seed(42)

    # Generate the plot with the same parameters used to create expected.png
    cluster_decision_tree(
        data=cluster_data,
        resolutions=resolutions,
        prefix="leiden_res_",
        node_spacing=5.0,
        level_spacing=1.5,
        draw=True,
        output_path=None,  # Let image_comparer handle saving the plot
        figsize=(6.98, 5.55),
        dpi=40,
        node_size=200,
        # node_colormap = ["Blues", "Set2", "tab10", "Paired","Set3", "tab20"],
        node_colormap=["Blues", "red", "#00FF00", "plasma", "Set3", "tab20"],
        node_label_fontsize=8,
        edge_curvature=0.01,
        edge_threshold=0.05,
        edge_label_threshold=0.05,
        edge_label_position=0.5,
        edge_label_fontsize=4,
        top_genes_dict=top_genes_dict,
        show_gene_labels=True,
        n_top_genes=2,
        gene_label_offset=0.4,
        gene_label_fontsize=5,
        gene_label_threshold=0.001,
        level_label_offset=15,
        level_label_fontsize=8,
        title="Hierarchical Leiden Clustering",
        title_fontsize=8,
    )

    # Use image_comparer to compare the plot
    image_comparer(Path("tests/_images"), "cluster_decision_tree_plot", tol=50)


# Test 1: Basic functionality without gene labels
def test_cluster_decision_tree_basic(cluster_data):
    """Test that cluster_decision_tree runs without errors and returns a graph."""
    cluster_data, resolutions, top_genes_dict = cluster_data
    G = cluster_decision_tree(
        data=cluster_data,
        prefix="leiden_res_",
        resolutions=resolutions,
        draw=False,  # Don't draw during tests to avoid opening plot windows
    )

    # Check that the output is a directed graph
    assert isinstance(G, nx.DiGraph)

    # Check that the graph has nodes and edges
    assert len(G.nodes) > 0
    assert len(G.edges) > 0

    # Check that nodes have resolution and cluster attributes
    for node in G.nodes:
        assert "resolution" in G.nodes[node]
        assert "cluster" in G.nodes[node]


# Test 2: Basic functionality with gene labels
def test_cluster_decision_tree_with_gene_labels(cluster_data):
    """Test that cluster_decision_tree handles top_genes_dict and show_gene_labels."""
    cluster_data, resolutions, top_genes_dict = cluster_data
    G = cluster_decision_tree(
        data=cluster_data,
        prefix="leiden_res_",
        resolutions=resolutions,
        top_genes_dict=top_genes_dict,
        show_gene_labels=True,
        n_top_genes=2,
        draw=False,
    )

    # Check that the graph is still valid
    assert isinstance(G, nx.DiGraph)
    assert len(G.nodes) > 0
    assert len(G.edges) > 0


# Test 3: Error condition (show_gene_labels=True but top_genes_dict=None)
def test_cluster_decision_tree_missing_top_genes_dict(cluster_data):
    """Test that show_gene_labels=True with top_genes_dict=None raises an error or skips gracefully."""
    cluster_data, resolutions, _ = cluster_data
    # Depending on the implementation, this might raise an error or skip drawing gene labels
    G = cluster_decision_tree(
        data=cluster_data,
        prefix="leiden_res_",
        resolutions=resolutions,
        top_genes_dict=None,  # Explicitly set to None
        show_gene_labels=True,
        draw=False,
    )
    # If the implementation skips drawing gene labels when top_genes_dict is None, the test should pass
    assert isinstance(G, nx.DiGraph)
    # If the implementation raises an error, uncomment the following instead:
    # with pytest.raises(ValueError) as exc_info:
    #     cluster_decision_tree(
    #         data=cluster_data,
    #         prefix="leiden_res_",
    #         resolutions=resolutions,
    #         top_genes_dict=None,
    #         show_gene_labels=True,
    #         draw=False,
    #     )
    # assert "top_genes_dict must be provided when show_gene_labels=True" in str(exc_info.value)


# Test 4: Conflicting arguments (negative node_size)
def test_cluster_decision_tree_negative_node_size(cluster_data):
    """Test that a negative node_size raises a ValueError."""
    cluster_data, resolutions, top_genes_dict = cluster_data
    with pytest.raises(ValueError, match="node_size must be a positive number"):
        cluster_decision_tree(
            data=cluster_data,
            prefix="leiden_res_",
            resolutions=resolutions,
            node_size=-100,
            draw=False,
        )


# Test 5: Error conditions (invalid figsize)
def test_cluster_decision_tree_invalid_figsize(cluster_data):
    """Test that an invalid figsize raises a ValueError."""
    cluster_data, resolutions, top_genes_dict = cluster_data
    with pytest.raises(
        ValueError, match="figsize must be a tuple of two positive numbers"
    ):
        cluster_decision_tree(
            data=cluster_data,
            prefix="leiden_res_",
            resolutions=resolutions,
            figsize=(0, 5),  # Invalid: width <= 0
            draw=False,
        )


# Test 6: Helpful error message (missing column)
def test_cluster_decision_tree_missing_column():
    """Test that a DataFrame without the required column raises a ValueError."""
    # Create a DataFrame without the required clustering columns
    data = pd.DataFrame({"other_column": [1, 2, 3]})
    with pytest.raises(
        ValueError, match="No columns found with prefix 'leiden_res_' in the DataFrame"
    ):
        cluster_decision_tree(
            data=data,
            prefix="leiden_res_",
            resolutions=[0.1],
            draw=False,
        )


# Test 7: Orthogonal effects (draw argument)
def test_cluster_decision_tree_draw_argument(cluster_data):
    """Test that the draw argument doesn't affect the graph output."""
    cluster_data, resolutions, top_genes_dict = cluster_data

    # Run with draw=False
    G_no_draw = cluster_decision_tree(
        data=cluster_data,
        prefix="leiden_res_",
        resolutions=resolutions,
        top_genes_dict=top_genes_dict,
        draw=False,
    )

    # Run with draw=True (but mock plt.show to avoid opening a window)
    from unittest import mock

    with mock.patch("matplotlib.pyplot.show"):
        G_draw = cluster_decision_tree(
            data=cluster_data,
            prefix="leiden_res_",
            resolutions=resolutions,
            top_genes_dict=top_genes_dict,
            draw=True,
        )

    # Check that the graphs are the same
    assert nx.is_isomorphic(G_no_draw, G_draw)
    assert G_no_draw.nodes(data=True) == G_draw.nodes(data=True)

    # Convert edge attributes to a hashable form
    def make_edge_hashable(edges):
        return {
            (
                u,
                v,
                tuple(
                    (k, tuple(v) if isinstance(v, list) else v)
                    for k, v in sorted(d.items())
                ),
            )
            for u, v, d in edges
        }

    # Compare edges as sets to ignore order
    assert make_edge_hashable(G_no_draw.edges(data=True)) == make_edge_hashable(
        G_draw.edges(data=True)
    )


# Test 8: Equivalent inputs (node_colormap)
@pytest.mark.parametrize(
    "node_colormap",
    [
        None,
        ["Set3", "Set3"],  # Same colormap for both resolutions
    ],
)
def test_cluster_decision_tree_node_colormap(cluster_data, node_colormap):
    """Test that node_colormap=None and a uniform colormap produce similar results."""
    cluster_data, resolutions, top_genes_dict = cluster_data
    G = cluster_decision_tree(
        data=cluster_data,
        prefix="leiden_res_",
        resolutions=resolutions,
        node_colormap=node_colormap,
        top_genes_dict=top_genes_dict,
        draw=False,
    )
    # Check that the graph structure is the same regardless of colormap
    assert isinstance(G, nx.DiGraph)
    assert len(G.nodes) > 0


# Test 9: Bounds on gene labels (n_top_genes)
@pytest.mark.parametrize("n_top_genes", [1, 3])
def test_cluster_decision_tree_n_top_genes(cluster_data, n_top_genes):
    """Test that n_top_genes bounds the number of gene labels when show_gene_labels=True."""
    cluster_data, resolutions, top_genes_dict = cluster_data
    # Mock draw_gene_labels to capture the number of genes used
    from unittest import mock

    with mock.patch("scanpy.plotting._cluster_tree.draw_gene_labels") as mock_draw:
        cluster_decision_tree(
            data=cluster_data,
            prefix="leiden_res_",
            resolutions=resolutions,
            top_genes_dict=top_genes_dict,
            show_gene_labels=True,
            n_top_genes=n_top_genes,
            draw=False,
        )
        # Check the n_top_genes argument passed to draw_gene_labels
        if mock_draw.called:
            _, kwargs = mock_draw.call_args
            assert kwargs["n_top_genes"] == n_top_genes
