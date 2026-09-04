# -*- coding: utf-8 -*-
"""
语义向量模块 — 余弦距离意外程度评分、MMR多样性排序、PCA可视化、茧房质心漂移。
"""
import threading
import numpy as np

_model = None
_lock  = threading.Lock()

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                print(f"[embedder] loading {MODEL_NAME} ...")
                _model = SentenceTransformer(MODEL_NAME)
                print("[embedder] model ready")
    return _model


def embed(texts: list) -> np.ndarray:
    """返回 L2-normalized 向量矩阵，shape=(len(texts), dim)。"""
    return get_model().encode(texts, normalize_embeddings=True, show_progress_bar=False)


def surprise_score(bubble_emb: np.ndarray, article_emb: np.ndarray) -> int:
    """
    余弦距离 = 1 - dot(a, b)（已归一化，所以 dot = cosine similarity）。
    距离越大 → 文章与茧房越不同 → 意外程度越高。
    映射到 0-10：dist=0.25 → 0, dist=1.10 → 10
    """
    dist  = float(1.0 - np.dot(bubble_emb, article_emb))
    score = (dist - 0.25) / 0.85 * 10
    return max(0, min(10, round(score)))


def compute_centroid(texts: list) -> np.ndarray:
    """将多条文本 embed 后取平均，返回 L2-normalized 质心向量。"""
    embs = embed(texts)
    mean = embs.mean(axis=0)
    norm = np.linalg.norm(mean)
    return mean / norm if norm > 0 else mean


def update_centroid(current: np.ndarray, new_emb: np.ndarray, decay: float = 0.92) -> np.ndarray:
    """
    指数滑动平均：将新读文章的向量加权融合进茧房质心。
    decay=0.92 → 每次阅读使质心向新文章方向移动约 8%。
    """
    updated = current * decay + new_emb * (1.0 - decay)
    norm = np.linalg.norm(updated)
    return updated / norm if norm > 0 else updated


def mmr_select(bubble_emb: np.ndarray, candidates: list, k: int = 8, lam: float = 0.65) -> list:
    """
    Maximal Marginal Relevance：选 k 篇文章，平衡意外程度与语义多样性。
    lam 越高 → 偏向高分；越低 → 偏向多样性。
    候选项每个需包含 '_emb'（np.ndarray）和 'score'（int 0-10）字段。
    """
    if len(candidates) <= k:
        return candidates

    selected = []
    remaining = list(candidates)

    while len(selected) < k and remaining:
        if not selected:
            best = max(remaining, key=lambda x: x['score'])
        else:
            sel_embs = np.array([x['_emb'] for x in selected])
            def _score(item, se=sel_embs):
                surprise = item['score'] / 10.0
                max_sim = float(np.dot(se, item['_emb']).max())
                return lam * surprise - (1 - lam) * max_sim
            best = max(remaining, key=_score)
        selected.append(best)
        remaining.remove(best)

    return selected


def pca_2d(embeddings: np.ndarray) -> np.ndarray:
    """SVD-based PCA，将高维向量投影到 2D，返回 shape=(N, 2) 的坐标矩阵。"""
    n = len(embeddings)
    if n < 2:
        return np.zeros((n, 2))
    mean = embeddings.mean(axis=0)
    centered = embeddings - mean
    try:
        _, _, Vt = np.linalg.svd(centered, full_matrices=False)
        return (centered @ Vt[:2].T).astype(float)
    except Exception:
        return np.zeros((n, 2))


def prepare_viz_data(bubble_emb: np.ndarray, items: list) -> dict:
    """
    计算茧房质心 + 所有文章 embedding 的 2D PCA 坐标。
    将 pca_x / pca_y 直接写入每个 item dict（in-place）。
    返回茧房质心在 2D 平面上的坐标。
    """
    if not items:
        return {}
    embs = np.array([bubble_emb] + [item['_emb'] for item in items])
    coords = pca_2d(embs)
    for item, coord in zip(items, coords[1:]):
        item['pca_x'] = round(float(coord[0]), 4)
        item['pca_y'] = round(float(coord[1]), 4)
    return {
        "bubble_x": round(float(coords[0][0]), 4),
        "bubble_y": round(float(coords[0][1]), 4),
    }


def load_centroid(path: str):
    """从 .npy 文件加载质心向量，不存在或读取失败则返回 None。"""
    try:
        return np.load(path)
    except Exception:
        return None


def save_centroid(centroid: np.ndarray, path: str):
    """保存质心向量到 .npy 文件。"""
    np.save(path, centroid)
