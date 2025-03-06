"""
The implementation of Transformer for the partially-observed time-series forecasting task.

"""

# Created by Wenjie Du <wenjay.du@gmail.com>
# License: BSD-3-Clause

from typing import Union, Optional

import numpy as np
import torch
from torch.utils.data import DataLoader

try:
    import nni
except ImportError:
    pass

from .core import _FITS
from .data import DatasetForFITS
from ..base import BaseNNForecaster
from ...data.checking import key_in_data_set
from ...optim.adam import Adam
from ...optim.base import Optimizer


class FITS(BaseNNForecaster):
    """The PyTorch implementation of the FITS forecasting model :cite:`xu2024fits`.

    Parameters
    ----------
    n_steps :
        The number of time steps in the time-series data sample.

    n_features :
        The number of features in the time-series data sample.

    n_pred_steps :
        The number of steps in the forecasting time series.

    n_pred_features :
        The number of features in the forecasting time series.

    cut_freq :
        The cut-off frequency for the Fourier transformation.

    individual :
        Whether to use individual Fourier transformation for each feature.

    apply_nonstationary_norm :
        Whether to apply the non-stationary normalization to the input data.

    batch_size :
        The batch size for training and evaluating the model.

    epochs :
        The number of epochs for training the model.

    patience :
        The patience for the early-stopping mechanism. Given a positive integer, the training process will be
        stopped when the model does not perform better after that number of epochs.
        Leaving it default as None will disable the early-stopping.

    train_loss_func:
        The customized loss function designed by users for training the model.
        If not given, will use the default loss as claimed in the original paper.

    val_metric_func:
        The customized metric function designed by users for validating the model.
        If not given, will use the default MSE metric.

    optimizer :
        The optimizer for model training.
        If not given, will use a default Adam optimizer.

    num_workers :
        The number of subprocesses to use for data loading.
        `0` means data loading will be in the main process, i.e. there won't be subprocesses.

    device :
        The device for the model to run on. It can be a string, a :class:`torch.device` object, or a list of them.
        If not given, will try to use CUDA devices first (will use the default CUDA device if there are multiple),
        then CPUs, considering CUDA and CPU are so far the main devices for people to train ML models.
        If given a list of devices, e.g. ['cuda:0', 'cuda:1'], or [torch.device('cuda:0'), torch.device('cuda:1')] , the
        model will be parallely trained on the multiple devices (so far only support parallel training on CUDA devices).
        Other devices like Google TPU and Apple Silicon accelerator MPS may be added in the future.

    saving_path :
        The path for automatically saving model checkpoints and tensorboard files (i.e. loss values recorded during
        training into a tensorboard file). Will not save if not given.

    model_saving_strategy :
        The strategy to save model checkpoints. It has to be one of [None, "best", "better", "all"].
        No model will be saved when it is set as None.
        The "best" strategy will only automatically save the best model after the training finished.
        The "better" strategy will automatically save the model during training whenever the model performs
        better than in previous epochs.
        The "all" strategy will save every model after each epoch training.

    verbose :
        Whether to print out the training logs during the training process.
    """

    def __init__(
        self,
        n_steps: int,
        n_features: int,
        n_pred_steps: int,
        n_pred_features: int,
        cut_freq: int,
        individual: bool = False,
        apply_nonstationary_norm: bool = False,
        batch_size: int = 32,
        epochs: int = 100,
        patience: Optional[int] = None,
        train_loss_func: Optional[dict] = None,
        val_metric_func: Optional[dict] = None,
        optimizer: Optional[Optimizer] = Adam(),
        num_workers: int = 0,
        device: Optional[Union[str, torch.device, list]] = None,
        saving_path: Optional[str] = None,
        model_saving_strategy: Optional[str] = "best",
        verbose: bool = True,
    ):
        super().__init__(
            batch_size=batch_size,
            epochs=epochs,
            patience=patience,
            train_loss_func=train_loss_func,
            val_metric_func=val_metric_func,
            num_workers=num_workers,
            device=device,
            saving_path=saving_path,
            model_saving_strategy=model_saving_strategy,
            verbose=verbose,
        )

        self.n_steps = n_steps
        self.n_features = n_features
        self.n_pred_steps = n_pred_steps
        self.n_pred_features = n_pred_features
        self.cut_freq = cut_freq
        self.individual = individual
        self.apply_nonstationary_norm = apply_nonstationary_norm

        # set up the model
        self.model = _FITS(
            self.n_steps,
            self.n_features,
            self.n_pred_steps,
            self.n_pred_features,
            self.cut_freq,
            self.individual,
            self.apply_nonstationary_norm,
        )
        self._print_model_size()
        self._send_model_to_given_device()

        # set up the optimizer
        self.optimizer = optimizer
        self.optimizer.init_optimizer(self.model.parameters())

    def _assemble_input_for_training(self, data: list) -> dict:
        (
            indices,
            X,
            missing_mask,
            X_pred,
            X_pred_missing_mask,
        ) = self._send_data_to_given_device(data)

        inputs = {
            "X": X,
            "missing_mask": missing_mask,
            "X_pred": X_pred,
            "X_pred_missing_mask": X_pred_missing_mask,
        }
        return inputs

    def _assemble_input_for_validating(self, data: list) -> dict:
        return self._assemble_input_for_training(data)

    def _assemble_input_for_testing(self, data: list) -> dict:
        (
            indices,
            X,
            missing_mask,
        ) = self._send_data_to_given_device(data)

        inputs = {
            "X": X,
            "missing_mask": missing_mask,
        }
        return inputs

    def fit(
        self,
        train_set: Union[dict, str],
        val_set: Optional[Union[dict, str]] = None,
        file_type: str = "hdf5",
    ) -> None:
        # Step 1: wrap the input data with classes Dataset and DataLoader
        training_set = DatasetForFITS(
            train_set,
            file_type=file_type,
        )
        training_loader = DataLoader(
            training_set,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
        )
        val_loader = None
        if val_set is not None:
            if not key_in_data_set("X_pred", val_set):
                raise ValueError("val_set must contain 'X_pred' for model validation.")
            val_set = DatasetForFITS(
                val_set,
                file_type=file_type,
            )
            val_loader = DataLoader(
                val_set,
                batch_size=self.batch_size,
                shuffle=False,
                num_workers=self.num_workers,
            )

        # Step 2: train the model and freeze it
        self._train_model(training_loader, val_loader)
        self.model.load_state_dict(self.best_model_dict)
        self.model.eval()  # set the model as eval status to freeze it.

        # Step 3: save the model if necessary
        self._auto_save_model_if_necessary(confirm_saving=self.model_saving_strategy == "best")

    def predict(
        self,
        test_set: Union[dict, str],
        file_type: str = "hdf5",
    ) -> dict:
        """

        Parameters
        ----------
        test_set : dict or str
            The dataset for model validating, should be a dictionary including keys as 'X' and 'y',
            or a path string locating a data file.
            If it is a dict, X should be array-like of shape [n_samples, sequence length (n_steps), n_features],
            which is time-series data for validating, can contain missing values, and y should be array-like of shape
            [n_samples], which is classification labels of X.
            If it is a path string, the path should point to a data file, e.g. a h5 file, which contains
            key-value pairs like a dict, and it has to include keys as 'X' and 'y'.

        file_type :
            The type of the given file if test_set is a path string.

        Returns
        -------
        result_dict: dict
            Prediction results in a Python Dictionary for the given samples.
            It should be a dictionary including a key named 'imputation'.

        """

        # Step 1: wrap the input data with classes Dataset and DataLoader
        self.model.eval()  # set the model as eval status to freeze it.
        test_set = DatasetForFITS(
            test_set,
            return_X_pred=False,
            file_type=file_type,
        )

        test_loader = DataLoader(
            test_set,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
        )
        forecasting_collector = []

        # Step 2: process the data with the model
        with torch.no_grad():
            for idx, data in enumerate(test_loader):
                inputs = self._assemble_input_for_testing(data)
                results = self.model(inputs)
                forecasting_data = results["forecasting_data"]
                forecasting_collector.append(forecasting_data)

        # Step 3: output collection and return
        forecasting_data = torch.cat(forecasting_collector).cpu().detach().numpy()
        result_dict = {
            "forecasting": forecasting_data,  # [bz, n_pred_steps, n_features]
        }
        return result_dict

    def forecast(
        self,
        test_set: Union[dict, str],
        file_type: str = "hdf5",
    ) -> np.ndarray:
        """Forecast the future of the input with the trained model.

        Parameters
        ----------
        test_set :
            The data samples for testing, should be array-like of shape [n_samples, sequence length (n_steps),
            n_features], or a path string locating a data file, e.g. h5 file.

        file_type :
            The type of the given file if X is a path string.

        Returns
        -------
        array-like, shape [n_samples, n_pred_steps, n_features],
            Forecasting results.
        """

        result_dict = self.predict(test_set, file_type=file_type)
        return result_dict["forecasting"]
