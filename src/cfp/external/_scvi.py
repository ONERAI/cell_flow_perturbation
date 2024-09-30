from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

import jax.numpy as jnp
import numpy as np
from scvi.model import JaxSCVI

from cfp._types import ArrayLike
from cfp.external._scvi_utils import CFJaxVAE

if TYPE_CHECKING:
    from typing import Literal

    from anndata import AnnData

logger = logging.getLogger(__name__)

__all__ = ["CFJaxSCVI"]


class CFJaxSCVI(JaxSCVI):

    _module_cls = CFJaxVAE

    def __init__(
        self,
        adata: AnnData,
        n_hidden: int = 128,
        n_latent: int = 10,
        dropout_rate: float = 0.1,
        gene_likelihood: Literal["nb", "poisson", "normal"] = "normal",
        **model_kwargs,
    ):
        super().__init__(adata)

        n_batch = self.summary_stats.n_batch

        self.module = self._module_cls(
            n_input=self.summary_stats.n_vars,
            n_batch=n_batch,
            n_hidden=n_hidden,
            n_latent=n_latent,
            dropout_rate=dropout_rate,
            gene_likelihood=gene_likelihood,
            **model_kwargs,
        )

        self._model_summary_string = ""
        self.init_params_ = self._get_init_params(locals())

    def get_reconstructed_expression(
        self,
        adata: AnnData,
        use_rep: str = "X_scVI",
        indices: Sequence[int] | None = None,
        give_mean: bool = False,
        batch_size: int | None = 10240,
    ) -> ArrayLike:
        r"""
        Return the reconstructed expression for each cell.

        This is denoted as :math:`z_n` in our manuscripts.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        use_rep
            Key for `.obsm` that contains the latent representation to use.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        give_mean
            Whether to return the mean of the negative binomial distribution or the
            unscaled expression.
        n_samples
            Number of samples to use for computing the latent representation.
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.

        Returns
        -------
        reconstructed_expression : np.ndarray
        """
        if batch_size is None:
            batch_size = adata.obsm[use_rep].shape[0]

        self._check_if_trained(warn=False)

        adata = self._validate_anndata(adata)
        scdl = self._make_data_loader(
            adata=adata, indices=indices, batch_size=batch_size, iter_ndarray=True
        )

        jit_generative_fn = self.module.get_jit_generative_fn()
        # Make dummy dict to conform with scVI functions
        # inference_outputs = {"z": adata.obsm[use_rep]}
        # We also have to batch over z here, so we split it in batches
        split_indixes = np.arange(0, adata.obsm[use_rep].shape[0], batch_size)
        recon = []
        for array_dict, z_idx in zip(scdl, split_indixes, strict=False):
            z_batch = adata.obsm[use_rep][z_idx : z_idx + batch_size, :]
            # Make dummy dict to conform with scVI functions
            inference_outputs = {"z": z_batch}
            out = jit_generative_fn(self.module.rngs, array_dict, inference_outputs)
            if give_mean and self.module.gene_likelihood != "normal":
                x = out["px"].mean
            else:
                x = out["rho"]
            recon.append(x)
        recon = jnp.concatenate(recon, axis=0)

        return self.module.as_numpy_array(recon)
