import copy
import os
import warnings

import dill
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from metal.contrib.modules.lstm_module import EmbeddingsEncoder, LSTMModule
from metal.end_model import IdentityModule
from metal.mmtl.glue.glue_auxiliary_tasks import SPACY_TAGS, auxiliary_task_functions
from metal.mmtl.glue.glue_datasets import get_glue_dataset
from metal.mmtl.glue.glue_metrics import acc_f1, matthews_corr, mse, pearson_spearman
from metal.mmtl.glue.glue_modules import (
    BertExtractCls,
    BertRaw,
    BertTokenClassificationHead,
    BinaryHead,
    MulticlassHead,
    RegressionHead,
    SoftAttentionModule,
)
from metal.mmtl.glue.glue_slices import create_slice_labels
from metal.mmtl.payload import Payload
from metal.mmtl.scorer import Scorer
from metal.mmtl.slicing.tasks import create_slice_task
from metal.mmtl.task import ClassificationTask, RegressionTask
from metal.mmtl.token_task import TokenClassificationTask
from metal.utils import recursive_merge_dicts, set_seed

ALL_TASKS = [
    "COLA",
    "SST2",
    "MNLI",
    "SNLI",
    "RTE",
    "WNLI",
    "QQP",
    "MRPC",
    "STSB",
    "QNLI",
]

# List of tasks requiring Spacy tokenization
SPACY_TASKS = ["SPACY_NER", "SPACY_POS"]

task_defaults = {
    # General
    "attention": True,
    "split_prop": None,
    "splits": ["train", "valid", "test"],
    "max_len": 200,
    "max_datapoints": -1,
    "seed": None,
    "preprocessed": False,  # If True, load the cached datasets with spacy tokens saved
    "dl_kwargs": {
        "batch_size": 16,
        "shuffle": True,  # Used only when split_prop is None; otherwise, use Sampler
    },
    "task_dl_kwargs": None,  # Overwrites dl kwargs e.g. {"STSB": {"batch_size": 2}}
    # NOTE: This dropout only applies to the output of the pooler; it will not change
    # the dropout rate of BERT (defaults to 0.1) or add dropout to other modules.
    # The main BERT module ends with a dropout layer already, so token-based tasks
    # that do not use BertExtractCls middle module do not need additional dropout first
    "dropout": 0.1,
    # BERT
    "encoder_type": "bert",
    "bert_model": "bert-base-uncased",  # Required for all encoders for BertTokenizer
    "bert_kwargs": {
        "freeze_bert": False,
        "reinit_bert": False,
        "pooler": True,  # If True, include the [768, 768] linear on top of [CLS] token
    },
    # LSTM
    "lstm_config": {
        "emb_size": 300,
        "hidden_size": 512,
        "vocab_size": 30522,  # bert-base-uncased-vocab.txt
        "bidirectional": True,
        "lstm_num_layers": 1,
    },
    #
    # Auxiliary Tasks
    "auxiliary_task_dict": {  # A map of each aux. task to the payloads it applies to
        "THIRD": ALL_TASKS,
        "BLEU": ["MNLI", "RTE", "WNLI", "QQP", "MRPC", "STSB", "QNLI"],  # sent pairs
        "SPACY_NER": ALL_TASKS,
        "SPACY_POS": ALL_TASKS,
    },
    "auxiliary_loss_multiplier": 1.0,
    "tasks": None,  # Comma-sep task list e.g. QNLI,QQP
    # Slicing
    "slice_dict": None,  # A map of the slices that apply to each task
}


def create_glue_tasks_payloads(task_names, skip_payloads=False, **kwargs):
    assert len(task_names) > 0

    config = recursive_merge_dicts(task_defaults, kwargs)

    if config["seed"] is None:
        config["seed"] = np.random.randint(1e6)
        print(f"Using random seed: {config['seed']}")
    set_seed(config["seed"])

    # share bert encoder for all tasks

    if config["encoder_type"] == "bert":
        bert_kwargs = config["bert_kwargs"]
        bert_model = BertRaw(config["bert_model"], **bert_kwargs)
        if "base" in config["bert_model"]:
            neck_dim = 768
        elif "large" in config["bert_model"]:
            neck_dim = 1024
        input_module = bert_model
        pooler = bert_model.pooler if bert_kwargs["pooler"] else None
        cls_middle_module = BertExtractCls(pooler=pooler, dropout=config["dropout"])
    else:
        raise NotImplementedError

    # Create dict override dl_kwarg for specific task
    # e.g. {"STSB": {"batch_size": 2}}
    task_dl_kwargs = {}
    if config["task_dl_kwargs"]:
        task_configs_str = [
            tuple(config.split(".")) for config in config["task_dl_kwargs"].split(",")
        ]
        for (task_name, kwarg_key, kwarg_val) in task_configs_str:
            if kwarg_key == "batch_size":
                kwarg_val = int(kwarg_val)
            task_dl_kwargs[task_name] = {kwarg_key: kwarg_val}

    tasks = []
    payloads = []
    for task_name in task_names:
        # If a flag is specified for attention, use it, otherwise use identity module
        if config["attention"]:
            print("Using soft attention head")
            attention_module = SoftAttentionModule(neck_dim)
        else:
            attention_module = IdentityModule()

        # Pull out names of auxiliary tasks to be dealt with in a second step
        # TODO: fix this logic for cases where auxiliary task for task_name has
        # its own payload
        has_payload = task_name not in config["auxiliary_task_dict"]

        # Note whether this task has auxiliary tasks that apply to it and require spacy
        run_spacy = False
        for aux_task, target_payloads in config["auxiliary_task_dict"].items():
            run_spacy = run_spacy or (
                task_name in target_payloads
                and aux_task in SPACY_TASKS
                and aux_task in task_names
            )

        # Override general dl kwargs with task-specific kwargs
        dl_kwargs = copy.deepcopy(config["dl_kwargs"])
        if task_name in task_dl_kwargs:
            dl_kwargs.update(task_dl_kwargs[task_name])

        # Each primary task has data_loaders to load
        if has_payload and not skip_payloads:
            if config["preprocessed"]:
                datasets = load_glue_datasets(
                    dataset_name=task_name,
                    splits=config["splits"],
                    bert_vocab=config["bert_model"],
                    max_len=config["max_len"],
                    max_datapoints=config["max_datapoints"],
                    run_spacy=run_spacy,
                    verbose=True,
                )
            else:
                datasets = create_glue_datasets(
                    dataset_name=task_name,
                    splits=config["splits"],
                    bert_vocab=config["bert_model"],
                    max_len=config["max_len"],
                    max_datapoints=config["max_datapoints"],
                    generate_uids=kwargs.get("generate_uids", False),
                    run_spacy=run_spacy,
                    verbose=True,
                )
            # Wrap datasets with DataLoader objects
            data_loaders = create_glue_dataloaders(
                datasets,
                dl_kwargs=dl_kwargs,
                split_prop=config["split_prop"],
                splits=config["splits"],
                seed=config["seed"],
            )

        if task_name == "COLA":
            scorer = Scorer(
                standard_metrics=["accuracy"],
                custom_metric_funcs={matthews_corr: ["matthews_corr"]},
            )
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=BinaryHead(neck_dim),
                scorer=scorer,
            )

        elif task_name == "SST2":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=BinaryHead(neck_dim),
            )

        elif task_name == "MNLI":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=MulticlassHead(neck_dim, 3),
                scorer=Scorer(standard_metrics=["accuracy"]),
            )

        elif task_name == "SNLI":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=MulticlassHead(neck_dim, 3),
                scorer=Scorer(standard_metrics=["accuracy"]),
            )

        elif task_name == "RTE":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=BinaryHead(neck_dim),
                scorer=Scorer(standard_metrics=["accuracy"]),
            )

        elif task_name == "WNLI":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=BinaryHead(neck_dim),
                scorer=Scorer(standard_metrics=["accuracy"]),
            )

        elif task_name == "QQP":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=BinaryHead(neck_dim),
                scorer=Scorer(
                    custom_metric_funcs={acc_f1: ["accuracy", "f1", "acc_f1"]}
                ),
            )

        elif task_name == "MRPC":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=BinaryHead(neck_dim),
                scorer=Scorer(
                    custom_metric_funcs={acc_f1: ["accuracy", "f1", "acc_f1"]}
                ),
            )

        elif task_name == "STSB":
            scorer = Scorer(
                standard_metrics=[],
                custom_metric_funcs={
                    pearson_spearman: [
                        "pearson_corr",
                        "spearman_corr",
                        "pearson_spearman",
                    ]
                },
            )

            task = RegressionTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=RegressionHead(neck_dim),
                scorer=scorer,
            )

        elif task_name == "QNLI":
            task = ClassificationTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=BinaryHead(neck_dim),
                scorer=Scorer(standard_metrics=["accuracy"]),
            )

        # AUXILIARY TASKS

        elif task_name == "THIRD":
            # A toy task that predict which third of the sentence each token is in
            OUT_DIM = 3
            task = TokenClassificationTask(
                name="THIRD",
                input_module=input_module,
                attention_module=attention_module,
                head_module=BertTokenClassificationHead(neck_dim, OUT_DIM),
                loss_multiplier=config["auxiliary_loss_multiplier"],
            )

        elif task_name == "BLEU":
            task = RegressionTask(
                name=task_name,
                input_module=input_module,
                middle_module=cls_middle_module,
                attention_module=attention_module,
                head_module=RegressionHead(neck_dim),
                output_hat_func=torch.sigmoid,
                loss_hat_func=(
                    lambda out, Y_gold: F.mse_loss(torch.sigmoid(out), Y_gold)
                ),
                scorer=Scorer(custom_metric_funcs={mse: ["mse"]}),
                loss_multiplier=config["auxiliary_loss_multiplier"],
            )

        elif task_name == "SPACY_NER":
            OUT_DIM = len(SPACY_TAGS["SPACY_NER"])
            task = TokenClassificationTask(
                name=task_name,
                input_module=input_module,
                attention_module=attention_module,
                head_module=BertTokenClassificationHead(neck_dim, OUT_DIM),
                loss_multiplier=config["auxiliary_loss_multiplier"],
            )

        elif task_name == "SPACY_POS":
            OUT_DIM = len(SPACY_TAGS["SPACY_POS"])
            task = TokenClassificationTask(
                name=task_name,
                input_module=input_module,
                attention_module=attention_module,
                head_module=BertTokenClassificationHead(neck_dim, OUT_DIM),
                loss_multiplier=config["auxiliary_loss_multiplier"],
            )

        else:
            msg = (
                f"Task name {task_name} was not recognized as a primary or "
                f"auxiliary task."
            )
            raise Exception(msg)

        tasks.append(task)

        # Gather slice names
        slice_names = (
            config["slice_dict"].get(task_name, []) if config["slice_dict"] else []
        )

        # Add a task for each slice
        for slice_name in slice_names:
            slice_task_name = f"{task_name}_slice:{slice_name}"
            slice_task = create_slice_task(task, slice_task_name)
            tasks.append(slice_task)

        if has_payload and not skip_payloads:
            # Create payloads (and add slices/auxiliary tasks as applicable)
            for split, data_loader in data_loaders.items():
                payload_name = f"{task_name}_{split}"
                labels_to_tasks = {f"{task_name}_gold": task_name}
                payload = Payload(payload_name, data_loader, labels_to_tasks, split)

                # Add auxiliary label sets if applicable
                auxiliary_task_dict = config["auxiliary_task_dict"]
                for aux_task_name, target_payloads in auxiliary_task_dict.items():
                    if aux_task_name in task_names and task_name in target_payloads:
                        aux_task_func = auxiliary_task_functions[aux_task_name]
                        payload = aux_task_func(payload)

                # Add a labelset slice to each split
                dataset = payload.data_loader.dataset
                for slice_name in slice_names:
                    slice_task_name = f"{task_name}_slice:{slice_name}"
                    slice_labels = create_slice_labels(
                        dataset, base_task_name=task_name, slice_name=slice_name
                    )
                    labelset_slice_name = f"{task_name}_slice:{slice_name}"
                    payload.add_label_set(
                        slice_task_name, labelset_slice_name, slice_labels
                    )

                payloads.append(payload)

    return tasks, payloads


def load_glue_datasets(
    dataset_name, splits, bert_vocab, max_len, max_datapoints, verbose=True
):
    bert_str = bert_vocab.replace("-", "_")
    filename = f"{dataset_name}_{bert_str}_spacy_datasets"
    filepath = f"{os.environ['GLUEDATA']}/datasets/{filename}.dill"
    if verbose:
        print(f"Loading preprocessed datasets for task {dataset_name} from {filepath}.")
    with open(filepath, "rb") as f:
        all_datasets = dill.load(f)

    datasets = {}
    for split, dataset in all_datasets.items():
        if split not in splits:
            continue
        datasets[split] = dataset

    if max_len != 200:
        warnings.warn("max_len for preprocessed data must be 200.")

    if max_datapoints > 0:
        warnings.warn("max_datapoints for preprocessed data must be -1")

    return datasets


def create_glue_datasets(
    dataset_name,
    splits,
    bert_vocab,
    max_len,
    max_datapoints,
    generate_uids=False,
    run_spacy=False,
    verbose=True,
):
    if verbose:
        print(f"Loading {dataset_name} Dataset")

    datasets = {}
    for split_name in splits:
        # Codebase uses valid but files are saved as dev.tsv
        if split_name == "valid":
            split = "dev"
        else:
            split = split_name
        datasets[split_name] = get_glue_dataset(
            dataset_name,
            split=split,
            bert_vocab=bert_vocab,
            max_len=max_len,
            max_datapoints=max_datapoints,
            generate_uids=generate_uids,
            run_spacy=run_spacy,
        )
    return datasets


def create_glue_dataloaders(datasets, dl_kwargs, split_prop, splits, seed=123):
    """ Initializes train/dev/test dataloaders given dataset_class"""
    dataloaders = {}

    # When split_prop is not None, we use create an artificial dev set from the train set
    if split_prop and "train" in splits:
        dataloaders["train"], dataloaders["valid"] = datasets["train"].get_dataloader(
            split_prop=split_prop, split_seed=seed, **dl_kwargs
        )

        # Use the dev set as test set if available.
        if "valid" in datasets:
            dataloaders["test"] = datasets["valid"].get_dataloader(**dl_kwargs)

    # When split_prop is None, we use standard train/dev/test splits.
    else:
        for split_name in datasets:
            dataloaders[split_name] = datasets[split_name].get_dataloader(**dl_kwargs)
    return dataloaders


### Code Graveyard (for code that we're just not ready to delete yet)
#
# elif config["encoder_type"] == "lstm":
#     # TODO: Allow these constants to be passed in as arguments
#     msg = (
#         "Non-BERT options are currently broken because of the BertExtractCls "
#         "hardcoded into most task heads."
#     )
#     raise NotImplementedError(msg)
#     lstm_config = config["lstm_config"]
#     neck_dim = lstm_config["hidden_size"]
#     if lstm_config["bidirectional"]:
#         neck_dim *= 2
#     lstm = LSTMModule(
#         lstm_config["emb_size"],
#         lstm_config["hidden_size"],
#         lstm_reduction="max",
#         bidirectional=lstm_config["bidirectional"],
#         lstm_num_layers=lstm_config["lstm_num_layers"],
#         encoder_class=EmbeddingsEncoder,
#         encoder_kwargs={"vocab_size": lstm_config["vocab_size"]},
#     )
#     input_module = lstm
