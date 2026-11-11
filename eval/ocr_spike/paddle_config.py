"""Build the PaddleOCR fine-tune config for PP-OCRv6_small_rec on the synthetic scorebug set (CPU)."""

from __future__ import annotations

from pathlib import Path

import yaml


def build_config(base_yml: Path, data_dir: Path, pretrained: Path, out_dir: Path, *, epochs: int = 1,
                 batch: int = 64, lr: float = 1e-4, workers: int = 4, eval_every: int = 500,
                 dict_path: str = "ppocr/utils/dict/ppocrv6_dict.txt") -> dict:
    cfg = yaml.safe_load(Path(base_yml).read_text(encoding="utf-8"))
    g = cfg["Global"]
    g.update({
        "use_gpu": False, "distributed": False, "epoch_num": epochs, "print_batch_step": 10,
        "save_model_dir": str(out_dir), "save_epoch_step": 1, "eval_batch_step": [0, eval_every],
        "cal_metric_during_train": True, "pretrained_model": str(pretrained).removesuffix(".pdparams"),
        "checkpoints": None, "character_dict_path": dict_path, "use_visualdl": False, "seed": 20261024,
        "save_res_path": str(out_dir / "predicts.txt"),
    })
    cfg["Optimizer"]["lr"] = {"name": "Cosine", "learning_rate": lr, "warmup_epoch": 0}
    resize = {"RecResizeImg": {"image_shape": [3, 48, 320]}}
    keep = {"KeepKeys": {"keep_keys": ["image", "label_ctc", "label_gtc", "length", "valid_ratio"]}}
    label = {"MultiLabelEncode": {"gtc_encode": "NRTRLabelEncode"}}
    decode = {"DecodeImage": {"img_mode": "BGR", "channel_first": False}}
    cfg["Train"] = {
        "dataset": {"name": "SimpleDataSet", "data_dir": str(data_dir), "label_file_list": [str(data_dir / "rec_train.txt")],
                    "transforms": [decode, {"RecAug": None}, label, resize, keep]},
        "loader": {"shuffle": True, "batch_size_per_card": batch, "drop_last": True, "num_workers": workers},
    }
    cfg["Eval"] = {
        "dataset": {"name": "SimpleDataSet", "data_dir": str(data_dir), "label_file_list": [str(data_dir / "rec_val_small.txt")],
                    "transforms": [decode, label, resize, keep]},
        "loader": {"shuffle": False, "drop_last": False, "batch_size_per_card": 128, "num_workers": 2},
    }
    return cfg


def write_config(cfg: dict, path: Path) -> None:
    Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
