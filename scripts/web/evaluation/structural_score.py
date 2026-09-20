"""Structural Score (SS) — continuous DOM-tree similarity for RecreationBench Web.

The core graph metric compares normalized DOM structure while preserving hierarchy
and element semantics. The site-level wrapper reads ``dom_snapshot.json`` files
(``{title, body, url}``) and scores the **body** subtree, with graceful degradation
when reference or agent DOM is missing.

The dimension that consumes this blends the continuous DOM similarity (primary)
with the tightened binary structural tests (secondary) — see evaluation.evaluator.
"""
import json
import logging
import re
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_dom(node: dict) -> dict:
    if not node or not isinstance(node, dict):
        return None
    skip_tags = {'script', 'style', 'noscript', 'template', 'svg', 'link', 'meta', 'iframe'}
    if node.get('tag') in skip_tags:
        return None
    artifact_ids = {'onetrust-consent-sdk', 'onetrust-banner-sdk', '__NEXT_DATA__', 'google_tag_manager', 'fb-root', 'CybotCookiebotDialog'}
    if node.get('id') in artifact_ids:
        return None
    classes = node.get('classes', [])
    artifact_prefixes = ('otFlat', 'ot-sdk', 'gtm-', 'cookie', 'consent')
    if any(cls.startswith(artifact_prefixes) for cls in classes):
        return None
    clean_classes = [c for c in classes if not re.match(r'^(sc-|css-|svelte-|styled-)[a-zA-Z0-9]', c) and not re.match(r'^_[a-zA-Z0-9]{5,}$', c)]

    children = []
    for child in node.get('children', []):
        if child.get('type') == 'text':
            content = child.get('content', '').strip()
            if content:
                children.append({'type': 'text', 'content': content})
        else:
            normalized = normalize_dom(child)
            if normalized:
                children.append(normalized)

    tag = node.get('tag', 'div')
    if tag == 'div' and not node.get('id') and not clean_classes and len(children) == 1 and children[0].get('tag'):
        return children[0]

    result = {'tag': tag}
    if node.get('id'):
        result['id'] = node['id']
    if clean_classes:
        result['classes'] = clean_classes
    attrs = {k: v for k, v in node.get('attributes', {}).items() if k in ('href', 'src', 'alt', 'role', 'aria-label')}
    if attrs:
        result['attributes'] = attrs
    if children:
        result['children'] = children
    return result


def flatten_tags(node: dict, tags=None) -> list:
    if tags is None:
        tags = []
    if not node or not isinstance(node, dict) or node.get('type') == 'text':
        return tags
    tag = node.get('tag', '')
    if tag:
        tags.append(tag)
    for child in node.get('children', []):
        flatten_tags(child, tags)
    return tags


def extract_text_tokens(node: dict, tokens=None) -> list:
    if tokens is None:
        tokens = []
    if not node or not isinstance(node, dict):
        return tokens
    if node.get('type') == 'text':
        tokens.extend(re.findall(r'\w+', node.get('content', '').lower()))
    else:
        for child in node.get('children', []):
            extract_text_tokens(child, tokens)
    return tokens


# ---------------------------------------------------------------------------
# TED with depth-decay weights and raised fallback threshold
# ---------------------------------------------------------------------------

def compute_tree_edit_distance_normalized(gt_dom: dict, agent_dom: dict) -> float:
    if not gt_dom and not agent_dom:
        return 1.0
    if not gt_dom or not agent_dom:
        return 0.0

    gt_tree = _build_tree(gt_dom)
    agent_tree = _build_tree(agent_dom)

    distance = _sequence_edit_distance(gt_tree, agent_tree)

    # Normalize by the weighted max possible distance (deleting all nodes from the larger tree)
    larger_tree = gt_tree if len(gt_tree) >= len(agent_tree) else agent_tree
    max_weighted_cost = sum(_depth_weight(t[2]) for t in larger_tree)
    if max_weighted_cost == 0:
        return 1.0
    return max(0.0, 1.0 - (distance / max_weighted_cost))


def _build_tree(node: dict, tree=None, parent_idx=-1, depth=0) -> list:
    """Build pre-order traversal with (tag, parent_idx, depth) tuples."""
    if tree is None:
        tree = []
    if not node or not isinstance(node, dict) or node.get('type') == 'text':
        return tree

    tag = node.get('tag', 'div')
    current_idx = len(tree)
    tree.append((tag, parent_idx, depth))

    for child in node.get('children', []):
        _build_tree(child, tree, current_idx, depth + 1)

    return tree


def _sequence_edit_distance(tree1: list, tree2: list) -> float:
    """Sequence edit distance with depth-decay weighting.

    Deeper nodes contribute less to the total distance, preventing
    score collapse when trees have very different depths.
    """
    n1, n2 = len(tree1), len(tree2)

    # Raised threshold from 1000 to 5000 to avoid LCS fallback
    if n1 > 5000 or n2 > 5000:
        tags1 = [t[0] for t in tree1]
        tags2 = [t[0] for t in tree2]
        lcs = _lcs_length(tags1, tags2)
        return max(n1, n2) - lcs

    if n1 == 0:
        return n2
    if n2 == 0:
        return n1

    dp = [[0.0] * (n2 + 1) for _ in range(n1 + 1)]

    # Base cases with depth-decay for insertions/deletions
    for i in range(1, n1 + 1):
        depth_i = tree1[i - 1][2]
        dp[i][0] = dp[i - 1][0] + _depth_weight(depth_i)
    for j in range(1, n2 + 1):
        depth_j = tree2[j - 1][2]
        dp[0][j] = dp[0][j - 1] + _depth_weight(depth_j)

    for i in range(1, n1 + 1):
        for j in range(1, n2 + 1):
            tag1, parent1, depth1 = tree1[i - 1]
            tag2, parent2, depth2 = tree2[j - 1]

            tag_cost = 0.0 if tag1 == tag2 else 1.0

            parent_tag1 = tree1[parent1][0] if 0 <= parent1 < n1 else ""
            parent_tag2 = tree2[parent2][0] if 0 <= parent2 < n2 else ""
            ctx_cost = 0.0 if parent_tag1 == parent_tag2 else 0.5

            avg_depth = (depth1 + depth2) / 2.0
            sub_cost = (tag_cost + ctx_cost) * _depth_weight(avg_depth)

            del_cost = _depth_weight(depth1)
            ins_cost = _depth_weight(depth2)

            dp[i][j] = min(
                dp[i - 1][j] + del_cost,
                dp[i][j - 1] + ins_cost,
                dp[i - 1][j - 1] + sub_cost,
            )

    return dp[n1][n2]


def _depth_weight(depth: float) -> float:
    """Decay with floor: shallow nodes matter more, but deep nodes still count."""
    return max(0.25, 1.0 / (1.0 + 0.2 * depth))


# ---------------------------------------------------------------------------
# Tag sequence match
# ---------------------------------------------------------------------------

def compute_tag_sequence_match(gt_dom: dict, agent_dom: dict) -> float:
    gt_tags = flatten_tags(gt_dom)
    agent_tags = flatten_tags(agent_dom)
    if not gt_tags and not agent_tags:
        return 1.0
    if not gt_tags or not agent_tags:
        return 0.0
    return _lcs_length(gt_tags, agent_tags) / max(len(gt_tags), len(agent_tags))


# ---------------------------------------------------------------------------
# Position-aware Text F1
# ---------------------------------------------------------------------------

def compute_text_f1(gt_dom: dict, agent_dom: dict) -> float:
    """Blended text F1: 0.6 * bag-of-words F1 + 0.4 * order-aware F1."""
    gt_tokens = extract_text_tokens(gt_dom)
    agent_tokens = extract_text_tokens(agent_dom)
    if not gt_tokens and not agent_tokens:
        return 1.0
    if not gt_tokens or not agent_tokens:
        return 0.0

    # Bag-of-words F1 (original)
    bag_f1 = _bag_of_words_f1(gt_tokens, agent_tokens)

    # Order-aware F1 via LCS
    order_f1 = _order_aware_f1(gt_tokens, agent_tokens)

    return 0.6 * bag_f1 + 0.4 * order_f1


def _bag_of_words_f1(gt_tokens: list, agent_tokens: list) -> float:
    gt_counts = Counter(gt_tokens)
    agent_counts = Counter(agent_tokens)
    common_count = sum((gt_counts & agent_counts).values())
    precision = common_count / sum(agent_counts.values()) if agent_tokens else 0.0
    recall = common_count / sum(gt_counts.values()) if gt_tokens else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _order_aware_f1(gt_tokens: list, agent_tokens: list) -> float:
    """F1 based on LCS length — measures how much content appears in correct order."""
    lcs_len = _lcs_length(gt_tokens, agent_tokens)
    precision = lcs_len / len(agent_tokens) if agent_tokens else 0.0
    recall = lcs_len / len(gt_tokens) if gt_tokens else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


# ---------------------------------------------------------------------------
# Semantic tag position verification
# ---------------------------------------------------------------------------

_SEMANTIC_POSITION_RULES = {
    'header': ('lt', 0.3),
    'nav': ('lt', 0.4),
    'footer': ('gt', 0.7),
}

def compute_semantic_tag_usage(gt_dom: dict, agent_dom: dict) -> float:
    """Semantic tag score: recall gated by position correctness.

    score = recall * (0.7 + 0.3 * position_correctness)
    This ensures position bonus is proportional to recall — can't get
    high scores just by placing a few tags correctly.
    """
    semantic_tags = {'header', 'nav', 'main', 'footer', 'section', 'article', 'aside'}

    gt_semantic = set(flatten_tags(gt_dom)) & semantic_tags
    agent_semantic = set(flatten_tags(agent_dom)) & semantic_tags

    if not gt_semantic:
        return 1.0

    matched = gt_semantic & agent_semantic
    recall = len(matched) / len(gt_semantic)

    if not matched:
        return 0.0

    position_score = _compute_position_correctness(agent_dom, matched)

    return recall * (0.7 + 0.3 * position_score)


def _compute_position_correctness(dom: dict, matched_tags: set) -> float:
    """Check whether matched semantic tags appear at reasonable DOM positions."""
    tags_with_position = _flatten_tags_with_position(dom)
    if not tags_with_position:
        return 1.0

    total_nodes = len(tags_with_position)
    tag_positions = {}
    for idx, tag in enumerate(tags_with_position):
        if tag in matched_tags and tag not in tag_positions:
            tag_positions[tag] = idx / total_nodes

    if not tag_positions:
        return 1.0

    correct = 0
    checked = 0
    for tag, rel_pos in tag_positions.items():
        if tag not in _SEMANTIC_POSITION_RULES:
            continue
        checked += 1
        direction, threshold = _SEMANTIC_POSITION_RULES[tag]
        if direction == 'lt' and rel_pos <= threshold:
            correct += 1
        elif direction == 'gt' and rel_pos >= threshold:
            correct += 1

    if checked == 0:
        return 1.0
    return correct / checked


def _flatten_tags_with_position(node: dict, tags=None) -> list:
    """Pre-order tag list for position computation."""
    if tags is None:
        tags = []
    if not node or not isinstance(node, dict) or node.get('type') == 'text':
        return tags
    tag = node.get('tag', '')
    if tag:
        tags.append(tag)
    for child in node.get('children', []):
        _flatten_tags_with_position(child, tags)
    return tags


# ---------------------------------------------------------------------------
# Unified scoring with rebalanced weights
# ---------------------------------------------------------------------------

def compute_structural_score(gt_dom: dict, agent_dom: dict, spec_scores: dict | None = None) -> dict:
    gt_norm = normalize_dom(gt_dom) or {}
    agent_norm = normalize_dom(agent_dom) or {}

    ted = compute_tree_edit_distance_normalized(gt_norm, agent_norm)
    tag_seq = compute_tag_sequence_match(gt_norm, agent_norm)
    text_f1 = compute_text_f1(gt_norm, agent_norm)
    semantic = compute_semantic_tag_usage(gt_norm, agent_norm)

    # Combined structure metric: TED (context-aware) weighted more than tag_seq
    structure = 0.6 * ted + 0.4 * tag_seq

    # Final score: structure 30%, text 45%, semantic 25%
    score = 0.30 * structure + 0.45 * text_f1 + 0.25 * semantic

    result = {
        "score": score,
        "ted": ted,
        "tag_sequence": tag_seq,
        "text_f1": text_f1,
        "semantic": semantic,
        "structure_combined": structure,
    }

    if spec_scores:
        content = spec_scores.get("content_spec_score", {}).get("score")
        semantic_spec = spec_scores.get("semantic_spec_score", {}).get("score")
        layout_spec = spec_scores.get("layout_spec_score", {}).get("score")
        result["score"] = (
            0.5 * score
            + 0.2 * (semantic_spec if semantic_spec is not None else semantic)
            + 0.2 * (content if content is not None else text_f1)
            + 0.1 * (layout_spec if layout_spec is not None else score)
        )
        result["spec_blend"] = {"content": content, "semantic": semantic_spec, "layout": layout_spec}

    return result


# ---------------------------------------------------------------------------
# Site-level wrapper: read dom_snapshot.json and score the body subtree
# ---------------------------------------------------------------------------

def _load_body(path: Path) -> dict | None:
    """Load a dom_snapshot.json and return its ``body`` subtree (the actual
    DOM tree). Returns None if the file is missing/unreadable or has no body."""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load dom_snapshot %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    body = data.get("body", data)
    return body if isinstance(body, dict) else None


def compute_structural_score_for_site(gt_dom_dir, agent_dom_dir, target_pages) -> dict:
    """Continuous DOM-similarity structural score over a site's target pages.

    For each page, compares the agent's captured DOM (agent_dom_dir/<page>/
    dom_snapshot.json) to GT's (gt_dom_dir/<page>/dom_snapshot.json), scoring the
    ``body`` subtree with compute_structural_score.

    Graceful degradation (mirrors visual_score policy):
      - GT DOM missing for a page  -> page skipped (not penalized; absent GT).
      - agent DOM missing (GT present) -> page scored 0.0 (agent failed to render).
      - no page scorable at all     -> overall_score=None (caller falls back to
                                        the binary structural test pass-rate).

    Returns {overall_score: float|None, n_scored: int, pages: {pid: {...}}}.
    """
    gt_dom_dir = Path(gt_dom_dir)
    agent_dom_dir = Path(agent_dom_dir)
    pages: dict = {}
    scored: list[float] = []

    for pid in target_pages:
        gt_body = _load_body(gt_dom_dir / pid / "dom_snapshot.json")
        if gt_body is None:
            pages[pid] = {"score": None, "reason": "no_gt_dom"}
            continue
        ag_body = _load_body(agent_dom_dir / pid / "dom_snapshot.json")
        if ag_body is None:
            pages[pid] = {"score": 0.0, "reason": "no_agent_dom"}
            scored.append(0.0)
            continue
        try:
            r = compute_structural_score(gt_body, ag_body)
        except Exception as e:
            logger.warning("structural score failed for %s: %s", pid, e)
            pages[pid] = {"score": None, "reason": f"error: {e}"}
            continue
        pages[pid] = {
            "score": round(r["score"], 4),
            "ted": round(r["ted"], 4),
            "tag_sequence": round(r["tag_sequence"], 4),
            "text_f1": round(r["text_f1"], 4),
            "semantic": round(r["semantic"], 4),
        }
        scored.append(r["score"])

    overall = (sum(scored) / len(scored)) if scored else None
    return {
        "overall_score": (round(overall, 4) if overall is not None else None),
        "n_scored": len(scored),
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# LCS utilities
# ---------------------------------------------------------------------------

def _lcs_length(a: list, b: list) -> int:
    if not a or not b:
        return 0
    if len(a) * len(b) > 200000:
        return _approximate_lcs(a, b)
    dp = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        prev = 0
        for j in range(1, len(b) + 1):
            temp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev + 1
            else:
                dp[j] = max(dp[j], dp[j - 1])
            prev = temp
    return dp[-1]


def _approximate_lcs(a: list, b: list) -> int:
    positions = {}
    for idx, token in enumerate(b):
        positions.setdefault(token, []).append(idx)
    matched = 0
    current = -1
    for token in a:
        for pos in positions.get(token, []):
            if pos > current:
                current = pos
                matched += 1
                break
    return matched
