import logging
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

import torch
from lightning import pytorch as pl
from lightning.pytorch.loggers import CSVLogger, TensorBoardLogger
from ray import tune
from ray.train import CheckpointConfig, RunConfig, ScalingConfig
from ray.train.lightning import (RayDDPStrategy, RayLightningEnvironment,
                                 RayTrainReportCallback, prepare_trainer)
from ray.train.torch import TorchTrainer
from ray.tune.schedulers import ASHAScheduler

from chemprop.cli.common import (add_common_args, process_common_args,
                                 validate_common_args)
from chemprop.cli.train import (add_train_args, build_datasets, build_model,
                                build_splits, normalize_inputs,
                                process_train_args, validate_train_args)
from chemprop.cli.utils.command import Subcommand
from chemprop.data import MolGraphDataLoader
from chemprop.nn import AggregationRegistry
from chemprop.nn.utils import Activation

logger = logging.getLogger(__name__)

class HyperoptSubcommand(Subcommand):
    COMMAND = "hyperopt"
    HELP = "perform hyperparameter optimization on the given task"

    @classmethod
    def add_args(cls, parser: ArgumentParser) -> ArgumentParser:
        parser = add_common_args(parser)
        parser = add_train_args(parser)
        return add_hyperopt_args(parser)

    @classmethod
    def func(cls, args: Namespace):
        args = process_common_args(args)
        args = process_train_args(args)
        validate_common_args(args)
        validate_train_args(args)
        main(args)

def add_hyperopt_args(parser: ArgumentParser) -> ArgumentParser:

    hyperopt_args = parser.add_argument_group("Hyperparameter optimization arguments")

    hyperopt_args.add_argument(
        "--num-samples",
        type=int,
        default=10,
        help="Number of hyperparameter optimization samples to run",
    )

    hyperopt_args.add_argument(
        "--hyperopt-seed",
        type=int,
        default=0,
        help="The initial seed used for choosing parameters in hyperparameter optimization trials. In each trial, the seed will be increased by one, skipping seeds previously used.",
    )

    hyperopt_args.add_argument(
        "--config-save-path",
        type=Path,
        help="Path to save the best hyperparameter configuration",
    )

    hyperopt_args.add_argument(
        "--hyperopt-checkpoint",
        type=Path,
        help="Path to a directory where hyperopt completed trial data is stored. Hyperopt job will include these trials if restarted. Can also be used to run multiple instances in parallel if they share the same checkpoint directory."
    )

    hyperopt_args.add_argument(
        "--startup-random-iters",
        type=int,
        help="The initial number of trials that will be randomly specified before TPE algorithm is used to select the rest. By default will be half the total number of trials.",
    )

    hyperopt_args.add_argument(
        "--manual-trial-dirs",
        type=Path,
        nargs="+",
        help="Paths to save directories for manually trained models in the same search space as the hyperparameter search. Results will be considered as part of the trial history of the hyperparameter search.",
    )

    hyperopt_args.add_argument(
        "--search-parameter-keywords",
        type=str,
        nargs="+",
        help=f"""The model parameters over which to search for an optimal hyperparameter configuration.
    Some options are bundles of parameters or otherwise special parameter operations.

    Special keywords:
        basic - the default set of hyperparameters for search: depth, ffn_num_layers, dropout, and linked_hidden_size.
        linked_hidden_size - search for hidden_size and ffn_hidden_size, but constrained for them to have the same value.
            If either of the component words are entered in separately, both are searched independently.
        learning_rate - search for max_lr, init_lr, final_lr, and warmup_epochs. The search for init_lr and final_lr values
            are defined as fractions of the max_lr value. The search for warmup_epochs is as a fraction of the total epochs used.
        all - include search for all 13 inidividual keyword options

    Individual supported parameters:
        {get_available_spaces(0).keys()}
    """,
    )

    hyperopt_args.add_argument(
        "--n-cpu-per-worker",
        type=int,
        default=1,
        help="Number of CPUs to allocate for each trial",
    )

    hyperopt_args.add_argument(
        "--n-gpu-per-worker",
        type=int,
        default=1,
        help="Number of GPUs to allocate for each trial",
    )

    hyperopt_args.add_argument(
        "--num-checkpoints-to-keep",
        type=int,
        default=1,
        help="Number of checkpoints to keep for each trial",
    )

    return parser

def get_available_spaces(train_epochs: int) -> dict:

    AVAILABLE_SPACES = {
        "activation": tune.choice(list(Activation.keys())),
        "aggregation": tune.choice(list(AggregationRegistry.keys())),
        "aggregation_norm": tune.quniform(lower=1, upper=200, q=1),
        "batch_size": tune.quniform(lower=5, upper=200, q=5),
        "depth": tune.quniform(lower=2, upper=6, q=1),
        "dropout": tune.choice(
            [
                tune.choice([0.]),
                tune.quniform(lower=0.05, upper=0.4, q=0.05),
            ],
        ),
        "ffn_hidden_size": tune.quniform(lower=300, upper=2400, q=100),
        "ffn_num_layers": tune.quniform(lower=2, upper=6, q=1),
        "final_lr_ratio": tune.loguniform(lower=1e-4, upper=1),
        "hidden_size": tune.quniform(lower=300, upper=2400, q=100),
        "init_lr_ratio": tune.loguniform(lower=1e-4, upper=1),
        "max_lr": tune.loguniform(lower=1e-6, upper=1e-2),
        "warmup_epochs": tune.quniform(lower=1, upper=train_epochs // 2, q=1)
    }

    return AVAILABLE_SPACES

def build_search_space(search_parameters: list[str], train_epochs: int) -> dict:
    AVAILABLE_SPACES = get_available_spaces(train_epochs)

    return {param: AVAILABLE_SPACES[param] for param in search_parameters}

def update_args_with_config(args: Namespace, config: dict) -> Namespace:
    for key, value in config.items():
        setattr(args, key, value)
    return args

def train_model(config, args, train_loader, val_loader, logger):

    update_args_with_config(args, config)

    model = build_model(args, train_loader.dataset)
    logger.info(model)

    monitor_mode = "min" if model.metrics[0].minimize else "max"
    logger.debug(f"Evaluation metric: '{model.metrics[0].alias}', mode: '{monitor_mode}'")

    trainer = pl.Trainer(
        accelerator="auto",
        devices=args.n_gpu if torch.cuda.is_available() else 1,
        strategy=RayDDPStrategy(),
        callbacks=[RayTrainReportCallback()],
        plugins=[RayLightningEnvironment()],
        gradient_clip_val=args.grad_clip,
    )
    train_loader = prepare_trainer(trainer)
    trainer.fit(model, train_loader, val_loader)

def tune_model(args, train_loader, val_loader, logger, monitor_mode):

    scheduler = ASHAScheduler(
        max_t=args.epochs,
        grace_period=1,
        reduction_factor=2,
    )

    scaling_config = ScalingConfig(
        num_workers=args.num_workers,
        use_gpu=args.n_gpu > 0,
        resources_per_worker={"cpu": args.n_cpu_per_worker, "gpu": args.n_gpu_per_worker},
    )

    checkpoint_config = CheckpointConfig(
        num_to_keep=args.num_checkpoints_to_keep,
        checkpoint_score_attribute="val_loss",
        checkpoint_score_order=monitor_mode,
    )

    run_config = RunConfig(
        checkpoint_config=checkpoint_config,
    )

    ray_trainer = TorchTrainer(
        lambda config: train_model(config, args, train_loader, val_loader, logger),
        scaling_config=scaling_config,
        run_config=run_config,
    )

    tune_config = tune.TuneConfig(
        metric="val_loss",
        mode=monitor_mode,
        num_samples=args.num_samples,
        scheduler=scheduler,
    )

    tuner = tune.Tuner(
        ray_trainer,
        param_space={"train_loop_config": build_search_space(args.search_parameter_keywords, args.epochs)},
        tune_config=tune_config,
    )

    return tuner.fit()

def main(args: Namespace):

    format_kwargs = dict(
        no_header_row=args.no_header_row,
        smiles_cols=args.smiles_columns,
        rxn_cols=args.reaction_columns,
        target_cols=args.target_columns,
        ignore_cols=args.ignore_columns,
        weight_col=args.weight_column,
        bounded=args.loss_function is not None and "bounded" in args.loss_function,
    )
    featurization_kwargs = dict(
        features_generators=args.features_generators, keep_h=args.keep_h, add_h=args.add_h
    )

    output_dir = args.output_dir
    output_dir.mkdir(exist_ok=True, parents=True)

    train_data, val_data, test_data = build_splits(args, format_kwargs, featurization_kwargs)
    train_dset, val_dset, test_dset = build_datasets(args, train_data, val_data, test_data)

    X_d_scaler, V_f_scaler, E_f_scaler, V_d_scaler = normalize_inputs(
        train_dset, val_dset, args
    )
    input_scalers = {"X_d": X_d_scaler, "V_f": V_f_scaler, "E_f": E_f_scaler, "V_d": V_d_scaler}

    if "regression" in args.task_type:
        scaler = train_dset.normalize_targets()
        val_dset.normalize_targets(scaler)
        logger.info(f"Train data: mean = {scaler.mean_} | std = {scaler.scale_}")
    else:
        scaler = None

    train_loader = MolGraphDataLoader(
        train_dset, args.batch_size, args.num_workers, seed=args.data_seed
    )
    val_loader = MolGraphDataLoader(val_dset, args.batch_size, args.num_workers, shuffle=False)
    if test_dset is not None:
        test_loader = MolGraphDataLoader(
            test_dset, args.batch_size, args.num_workers, shuffle=False
        )
    else:
        test_loader = None

    torch.manual_seed(args.pytorch_seed)

    model = build_model(args, train_loader.dataset)
    monitor_mode = "min" if model.metrics[0].minimize else "max"

    results = tune_model(args, train_loader, val_loader, logger, monitor_mode)

    results.get_best_result(metric="val_loss", mode=monitor_mode)

    logger.info(f"Best hyperparameter configuration: {results.best_config}")



if __name__ == "__main__":
    # TODO: update this old code or remove it.
    parser = ArgumentParser()
    parser = HyperoptSubcommand.add_args(parser)

    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, force=True)
    args = parser.parse_args()
    HyperoptSubcommand.func(args)
