import argparse
import json
import math
import random
import re
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageEnhance, ImageFilter
from torch import nn
from torch.utils.data import DataLoader, Dataset


ROOT = Path(__file__).resolve().parent
DATASET_ROOT = ROOT / "training_data" / "gui_clicks"
DATASET_PATH = DATASET_ROOT / "dataset.jsonl"
MODEL_ROOT = DATASET_ROOT / "models"


def tokenize(text):
    return re.findall(r"[a-z0-9]+", (text or "").casefold())


def prompt_variants(prompt):
    prompt = (prompt or "").strip()
    variants = [prompt]
    lowered = prompt.casefold()
    replacements = [
        ("click the ", "press the "),
        ("click the ", "select the "),
        ("click ", "press "),
        ("click ", "select "),
    ]
    for old, new in replacements:
        if old in lowered:
            variants.append(re.sub(old, new, prompt, count=1, flags=re.IGNORECASE))
    if prompt.lower().startswith("click "):
        variants.append(prompt[6:])
    return list(dict.fromkeys(v for v in variants if v))


def load_rows():
    rows = []
    with DATASET_PATH.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_no"] = line_no
            image_path = DATASET_ROOT / row["image"]
            if image_path.exists():
                rows.append(row)
    return rows


def split_by_image(rows, test_fraction, seed):
    rng = random.Random(seed)
    images = sorted(set(row["image"] for row in rows))
    rng.shuffle(images)
    test_count = max(1, int(round(len(images) * test_fraction)))
    test_images = set(images[:test_count])
    train = [row for row in rows if row["image"] not in test_images]
    test = [row for row in rows if row["image"] in test_images]
    return train, test, sorted(test_images)


def build_vocab(rows, max_words):
    counts = Counter()
    for row in rows:
        for variant in prompt_variants(row["prompt"]):
            counts.update(tokenize(variant))
    vocab = {"<unk>": 0}
    for word, _count in counts.most_common(max_words - 1):
        vocab[word] = len(vocab)
    return vocab


def vectorize_prompt(prompt, vocab):
    vec = np.zeros(len(vocab), dtype=np.float32)
    for tok in tokenize(prompt):
        vec[vocab.get(tok, 0)] += 1.0
    total = vec.sum()
    if total > 0:
        vec /= total
    return vec


def augment_image(image, rng):
    if rng.random() < 0.75:
        image = ImageEnhance.Brightness(image).enhance(rng.uniform(0.82, 1.18))
    if rng.random() < 0.75:
        image = ImageEnhance.Contrast(image).enhance(rng.uniform(0.82, 1.18))
    if rng.random() < 0.35:
        image = ImageEnhance.Color(image).enhance(rng.uniform(0.85, 1.15))
    if rng.random() < 0.20:
        image = image.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.2, 0.7)))
    return image


class ClickDataset(Dataset):
    def __init__(self, rows, vocab, image_size=(224, 126), training=False, repeats=1, seed=0):
        self.items = []
        for row in rows:
            variants = prompt_variants(row["prompt"]) if training else [row["prompt"]]
            for variant in variants:
                for _ in range(repeats):
                    self.items.append((row, variant))
        self.vocab = vocab
        self.image_size = image_size
        self.training = training
        self.seed = seed
        self.cache = {}

    def __len__(self):
        return len(self.items)

    def load_image(self, path):
        if path not in self.cache:
            image = Image.open(path).convert("RGB")
            self.cache[path] = image
        return self.cache[path]

    def __getitem__(self, idx):
        row, prompt = self.items[idx]
        image_path = DATASET_ROOT / row["image"]
        image = self.load_image(image_path)
        rng = random.Random(self.seed + idx + int(time.time() * 1000) if self.training else self.seed + idx)
        if self.training:
            image = augment_image(image, rng)
        image = image.resize(self.image_size, Image.Resampling.BICUBIC)
        arr = np.asarray(image, dtype=np.float32) / 255.0
        arr = np.transpose(arr, (2, 0, 1))

        width = float((row.get("metadata") or {}).get("width") or self.load_image(image_path).width)
        height = float((row.get("metadata") or {}).get("height") or self.load_image(image_path).height)
        target = np.array(
            [
                float(row["action"]["x"]) / max(1.0, width - 1.0),
                float(row["action"]["y"]) / max(1.0, height - 1.0),
            ],
            dtype=np.float32,
        )
        text = vectorize_prompt(prompt, self.vocab)
        return {
            "image": torch.from_numpy(arr),
            "text": torch.from_numpy(text),
            "target": torch.from_numpy(target),
            "width": torch.tensor(width, dtype=torch.float32),
            "height": torch.tensor(height, dtype=torch.float32),
            "id": row.get("id", ""),
        }


class ClickNet(nn.Module):
    def __init__(self, vocab_size):
        super().__init__()
        self.image = nn.Sequential(
            nn.Conv2d(3, 16, 5, stride=2, padding=2),
            nn.BatchNorm2d(16),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 32, 3, stride=2, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, 3, stride=2, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 96, 3, stride=2, padding=1),
            nn.BatchNorm2d(96),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d((6, 10)),
            nn.Flatten(),
        )
        self.text = nn.Sequential(
            nn.Linear(vocab_size, 128),
            nn.ReLU(inplace=True),
            nn.Dropout(0.15),
            nn.Linear(128, 64),
            nn.ReLU(inplace=True),
        )
        self.head = nn.Sequential(
            nn.Linear(96 * 6 * 10 + 64, 512),
            nn.ReLU(inplace=True),
            nn.Dropout(0.20),
            nn.Linear(512, 128),
            nn.ReLU(inplace=True),
            nn.Linear(128, 2),
            nn.Sigmoid(),
        )

    def forward(self, image, text):
        image_features = self.image(image)
        text_features = self.text(text)
        return self.head(torch.cat([image_features, text_features], dim=1))


def evaluate(model, loader, device):
    model.eval()
    errors = []
    rows = []
    with torch.no_grad():
        for batch in loader:
            image = batch["image"].to(device)
            text = batch["text"].to(device)
            pred = model(image, text).cpu()
            target = batch["target"]
            width = batch["width"]
            height = batch["height"]
            pred_px = torch.stack([pred[:, 0] * (width - 1), pred[:, 1] * (height - 1)], dim=1)
            target_px = torch.stack([target[:, 0] * (width - 1), target[:, 1] * (height - 1)], dim=1)
            err = torch.linalg.vector_norm(pred_px - target_px, dim=1)
            for i, value in enumerate(err.tolist()):
                errors.append(value)
                rows.append(
                    {
                        "id": batch["id"][i],
                        "prediction": {
                            "x": round(float(pred_px[i, 0]), 2),
                            "y": round(float(pred_px[i, 1]), 2),
                        },
                        "target": {
                            "x": round(float(target_px[i, 0]), 2),
                            "y": round(float(target_px[i, 1]), 2),
                        },
                        "error_px": round(float(value), 2),
                    }
                )
    return errors, rows


def summarize(errors):
    if not errors:
        return {}
    return {
        "count": len(errors),
        "mean_error_px": round(statistics.mean(errors), 2),
        "median_error_px": round(statistics.median(errors), 2),
        "min_error_px": round(min(errors), 2),
        "max_error_px": round(max(errors), 2),
        "hit_rate_10px": round(sum(e <= 10 for e in errors) / len(errors), 3),
        "hit_rate_25px": round(sum(e <= 25 for e in errors) / len(errors), 3),
        "hit_rate_50px": round(sum(e <= 50 for e in errors) / len(errors), 3),
        "hit_rate_100px": round(sum(e <= 100 for e in errors) / len(errors), 3),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=35)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--test-fraction", type=float, default=0.2)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--max-words", type=int, default=600)
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)

    rows = load_rows()
    train_rows, test_rows, test_images = split_by_image(rows, args.test_fraction, args.seed)
    vocab = build_vocab(train_rows, args.max_words)
    train_ds = ClickDataset(train_rows, vocab, training=True, repeats=args.repeats, seed=args.seed)
    test_ds = ClickDataset(test_rows, vocab, training=False, repeats=1, seed=args.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = ClickNet(len(vocab)).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    loss_fn = nn.SmoothL1Loss(beta=0.04)
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    test_loader = DataLoader(test_ds, batch_size=args.batch_size, shuffle=False)

    print(f"rows={len(rows)} train_rows={len(train_rows)} test_rows={len(test_rows)}")
    print(f"train_items={len(train_ds)} test_items={len(test_ds)} vocab={len(vocab)} device={device}")
    print(f"test_images={len(test_images)}")

    best = None
    best_state = None
    for epoch in range(1, args.epochs + 1):
        model.train()
        losses = []
        for batch in train_loader:
            image = batch["image"].to(device)
            text = batch["text"].to(device)
            target = batch["target"].to(device)
            pred = model(image, text)
            loss = loss_fn(pred, target)
            opt.zero_grad()
            loss.backward()
            opt.step()
            losses.append(float(loss.detach().cpu()))
        errors, _rows = evaluate(model, test_loader, device)
        summary = summarize(errors)
        median = summary["median_error_px"]
        if best is None or median < best["median_error_px"]:
            best = summary
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        print(
            f"epoch={epoch:03d} loss={statistics.mean(losses):.5f} "
            f"median={summary['median_error_px']:.2f}px mean={summary['mean_error_px']:.2f}px "
            f"hit50={summary['hit_rate_50px']:.3f} hit100={summary['hit_rate_100px']:.3f}"
        )

    if best_state is not None:
        model.load_state_dict(best_state)
    errors, pred_rows = evaluate(model, test_loader, device)
    final = summarize(errors)
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_path = MODEL_ROOT / f"clicknet_{stamp}.pt"
    metrics_path = MODEL_ROOT / f"clicknet_{stamp}_metrics.json"
    preds_path = MODEL_ROOT / f"clicknet_{stamp}_predictions.jsonl"
    torch.save(
        {
            "model_state": model.state_dict(),
            "vocab": vocab,
            "image_size": (224, 126),
            "args": vars(args),
        },
        model_path,
    )
    metrics = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model_path": str(model_path),
        "dataset": str(DATASET_PATH),
        "rows": len(rows),
        "train_rows": len(train_rows),
        "test_rows": len(test_rows),
        "test_images": test_images,
        "device": str(device),
        "best_by_median": best,
        "final": final,
    }
    metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    with preds_path.open("w", encoding="utf-8") as handle:
        for row in pred_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("\nFINAL")
    print(json.dumps(metrics, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
