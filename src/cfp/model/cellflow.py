import os
from collections.abc import Callable, Sequence
from typing import Any, Literal

import anndata as ad
import cloudpickle
import jax
import optax
from numpy.typing import ArrayLike
from ott.neural.methods.flows import dynamics, genot, otfm
from ott.solvers import utils as solver_utils

from cfp.data.data import PerturbationData
from cfp.data.dataloader import CFSampler
from cfp.networks.velocity_field import ConditionalVelocityField
from cfp.training.trainer import CellFlowTrainer

__all__ = ["CellFlow"]


class CellFlow:
    """CellFlow model for perturbation prediction using Flow Matching.

    Args:
        adata: Anndata object.
        solver: Solver to use for training the model.

    """

    def __init__(self, adata: ad.AnnData, solver: Literal["otfm", "genot"] = "otfm"):

        self.adata = adata
        self.solver = solver
        self.dataloader = None
        self.trainer = None
        self._solver = None

    def prepare_data(
        self,
        cell_data: Literal["X"] | dict[str, str],
        control_key: str | Sequence[str, Any],
        obs_perturbation_covariates: Sequence[tuple[str, ...]] | None = None,
        uns_perturbation_covariates: (
            Sequence[dict[str, Sequence[str, ...] | str]] | None
        ) = None,
        split_covariates: Sequence[str] | None = None,
        **kwargs,
    ) -> None:
        """Prepare dataloader for training from anndata object.

        Args:
            adata: An :class:`~anndata.AnnData` object.
            cell_data: Where to read the cell data from. Either 'X', a key in adata.obsm or a dictionary of the form {attr: key}, where 'attr' is an attribute of the :class:`~anndata.AnnData` object and key is the 'key' in the corresponding key.
            control_key: Tuple of length 2 with first element defining the column in :class:`~anndata.AnnData` and second element defining the value in `adata.obs[control_data[0]]` used to define all control cells.
            split_covariates: Covariates in adata.obs to split all control cells into different control populations. The perturbed cells are also split according to these columns, but if an embedding for these covariates should be encoded in the model, the corresponding column should also be used in `obs_perturbation_covariates` or `uns_perturbation_covariates`.
            obs_perturbation_covariates: Tuples of covariates in adata.obs characterizing the perturbed cells (together with `split_covariates` and `uns_perturbation_covariates`) and encoded by the values as found in `adata.obs`. If a tuple contains more than
            one element, this is interpreted as a combination of covariates that should be treated as an unordered set.
            uns_perturbation_covariates: Dictionaries with keys in adata.uns[`UNS_KEY_CONDITION`] and values columns in adata.obs which characterize the perturbed cells (together with `split_covariates` and `obs_perturbation_covariates`) and encoded by the values as found in `adata.uns[`UNS_KEY_CONDITION`][uns_perturbation_covariates.keys()]`. If a value of the dictionary is a tuple with more than one element, this is interpreted as a combination of covariates that should be treated as an unordered set.

        Returns
        -------
            None

        """
        obs_perturbation_covariates = obs_perturbation_covariates or []
        uns_perturbation_covariates = uns_perturbation_covariates or {}
        split_covariates = split_covariates or []
        control_data = (
            control_key if isinstance(control_key, tuple) else (control_key, True)
        )

        self.pdata = PerturbationData.load_from_adata(
            self.adata,
            cell_data=cell_data,
            split_covariates=split_covariates,
            control_data=control_data,
            obs_perturbation_covariates=obs_perturbation_covariates,
            uns_perturbation_covariates=uns_perturbation_covariates,
        )

        self._condition_dim = self.pdata.condition_data.shape[-1]
        self._data_dim = self.pdata.cell_data.shape[-1]

    def prepare_model(
        self,
        condition_encoder: Literal["transformer", "deepset"] = "transformer",
        condition_embedding_dim: int = 32,
        condition_encoder_kwargs: dict[str, Any] | None = None,
        velocity_field_kwargs: dict[str, Any] | None = None,
        solver_kwargs: dict[str, Any] | None = None,
        flow: (
            dict[Literal["constant_noise", "schroedinger_bridge"], float] | None
        ) = None,
        match_fn: Callable[
            [ArrayLike, ArrayLike], ArrayLike
        ] = solver_utils.match_linear,
        optimizer: optax.GradientTransformation = optax.adam(1e-4),
        seed=0,
    ) -> None:
        """Prepare model for training.

        Args:
            condition_encoder: Encoder for the condition.
            condition_embedding_dim: Dimensions of the condition embedding.
            condition_encoder_kwargs: Keyword arguments for the condition encoder.
            time_encoder_dims: Dimensions of the time embedding.
            time_encoder_dropout: Dropout rate for the time embedding.
            hidden_dims: Dimensions of the hidden layers.
            hidden_dropout: Dropout rate for the hidden layers.
            decoder_dims: Dimensions of the output layers.
            decoder_dropout: Dropout rate for the output layers.
            flow: Flow to use for training. Shoudl be a dict with the form {"constant_noise": noise_val} or {"schroedinger_bridge": noise_val}.
            match_fn: Matching function.
            optimizer: Optimizer for training.
            seed: Random seed.

        Returns
        -------
            None
        """
        if self.pdata is None:
            raise ValueError(
                "Dataloader not initialized. Please call prepare_data first."
            )

        condition_encoder_kwargs = condition_encoder_kwargs or {}
        velocity_field_kwargs = velocity_field_kwargs or {}
        solver_kwargs = solver_kwargs or {}
        flow = flow or {"constant_noise": 0.0}

        vf = ConditionalVelocityField(
            output_dim=self._data_dim,
            condition_encoder=condition_encoder,
            condition_dim=self._condition_dim,
            condition_embedding_dim=condition_embedding_dim,
            max_set_size=self.pdata.max_combination_length,
            condition_encoder_kwargs=condition_encoder_kwargs,
            **velocity_field_kwargs,
        )

        flow, noise = list(flow.items())[0]
        if flow == "constant_noise":
            flow = dynamics.ConstantNoiseFlow(noise)
        elif flow == "bridge":
            flow = dynamics.BrownianBridge(noise)
        else:
            raise NotImplementedError(
                f"The key of `flow` must be `constant_noise` or `bridge` but found {flow.keys()[0]}."
            )
        if self.solver == "otfm":
            self._solver = otfm.OTFlowMatching(
                vf=vf,
                match_fn=match_fn,
                flow=dynamics.ConstantNoiseFlow(0.0),
                optimizer=optimizer,
                rng=jax.random.PRNGKey(seed),
                **solver_kwargs,
            )
        elif self.solver == "genot":
            self._solver = genot.GENOT(
                vf=vf,
                data_match_fn=match_fn,
                flow=dynamics.ConstantNoiseFlow(0.0),
                source_dim=self._data_dim,
                target_dim=self._data_dim,
                optimizer=optimizer,
                rng=jax.random.PRNGKey(seed),
                **solver_kwargs,
            )
        self.trainer = CellFlowTrainer(model=self._solver)

    def train(
        self,
        num_iterations: int,
        batch_size: int = 64,
        valid_freq: int = 10,
        callback_fn: (
            Callable[[otfm.OTFlowMatching | genot.GENOT, dict[str, Any]], Any] | None
        ) = None,
    ) -> None:
        """Train the model.

        Args:
            num_iterations: Number of iterations to train the model.
            valid_freq: Frequency of validation.
            callback_fn: Callback function.

        Returns
        -------
            None
        """
        if self.pdata is None:
            raise ValueError("Data not initialized. Please call prepare_data first.")

        if self.trainer is None:
            raise ValueError("Model not initialized. Please call prepare_model first.")

        self.dataloader = CFSampler(data=self.pdata, batch_size=batch_size)

        self.trainer.train(
            dataloader=self.dataloader,
            num_iterations=num_iterations,
            valid_freq=valid_freq,
            callback_fn=callback_fn,
        )

    def predict(
        self,
        adata: ad.AnnData,
    ) -> ArrayLike:
        """Predict perturbation.

        Args:
            adata: Anndata object.


        Returns
        -------
            Perturbation prediction.
        """
        pass

    def get_condition_embedding(
        self,
        adata: ad.AnnData,
    ) -> ArrayLike:
        """Get condition embedding.

        Args:
            adata: Anndata object.

        Returns
        -------
            Condition embedding.
        """
        pass

    def save(
        self,
        dir_path: str,
        file_prefix: str | None = None,
        overwrite: bool = False,
    ) -> None:
        """
        Save the model. Pickles the CellFlow class instance.

        Args:
        dir_path: Path to a directory, defaults to current directory
        file_prefix: Prefix to prepend to the file name.
        overwrite: Overwrite existing data or not.

        Returns
        -------
        None
        """
        file_name = (
            f"{file_prefix}_{self.__class__.__name__}.pkl"
            if file_prefix is not None
            else f"{self.__class__.__name__}.pkl"
        )
        file_dir = (
            os.path.join(dir_path, file_name) if dir_path is not None else file_name
        )

        if not overwrite and os.path.exists(file_dir):
            raise RuntimeError(
                f"Unable to save to an existing file `{file_dir}` use `overwrite=True` to overwrite it."
            )
        with open(file_dir, "wb") as f:
            cloudpickle.dump(self, f)
