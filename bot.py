#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B0T xSpam v.1 - LLM-Powered Comment Moderation

Uses Detoxify as a pre-filter to decide what needs analysis,
then Groq API (free tier) for intelligent context-aware toxicity detection.
"""

import os
import sys
import json
import time
import logging
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

# -------- env loading --------
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.getenv("ENV_FILE", ".env"))

# -------- reddit / http --------
import praw
import prawcore

# -------- LLM --------
from groq import Groq
from openai import OpenAI  # For x.ai Grok API (OpenAI-compatible)

# -------- discord optional --------
import urllib.request


# -------------------------------
# Enums
# -------------------------------

class Verdict(Enum):
    """Classification results from LLM analysis"""
    REPORT = "REPORT"           # Clearly toxic, should be reported
    BENIGN = "BENIGN"           # Not toxic, no action needed


# -------------------------------
# Reported Comments Tracking
# -------------------------------

TRACKING_FILE = "reported_comments.json"
BENIGN_TRACKING_FILE = "benign_analyzed.json"
BENIGN_TRACKING_MAX_AGE_HOURS = 48  # Auto-cleanup entries older than this
PIPELINE_STATS_FILE = "pipeline_stats.json"
PENDING_REVIEWS_FILE = "pending_reviews.json"  # Track Discord messages awaiting mod review

def load_tracked_comments() -> List[Dict]:
    """Load tracked comments from JSON file"""
    try:
        with open(TRACKING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {TRACKING_FILE}, starting fresh")
        return []

def save_tracked_comments(comments: List[Dict]) -> None:
    """Save tracked comments to JSON file"""
    with open(TRACKING_FILE, "w", encoding="utf-8") as f:
        json.dump(comments, f, indent=2)

def load_pipeline_stats() -> Dict:
    """Load persisted pipeline stats from JSON file"""
    try:
        with open(PIPELINE_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {PIPELINE_STATS_FILE}, starting fresh")
        return {}

def save_pipeline_stats(stats: Dict) -> None:
    """Save pipeline stats to JSON file"""
    with open(PIPELINE_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

def load_benign_analyzed() -> List[Dict]:
    """Load benign analyzed comments from JSON file"""
    try:
        with open(BENIGN_TRACKING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {BENIGN_TRACKING_FILE}, starting fresh")
        return []

def save_benign_analyzed(comments: List[Dict]) -> None:
    """Save benign analyzed comments to JSON file"""
    with open(BENIGN_TRACKING_FILE, "w", encoding="utf-8") as f:
        json.dump(comments, f, indent=2)

def track_benign_analyzed(comment_id: str, permalink: str, text: str,
                          llm_reason: str, detoxify_score: float,
                          detoxify_scores: Dict[str, float],
                          is_top_level: bool = False,
                          prefilter_trigger: str = "",
                          all_ml_scores: Dict[str, float] = None,
                          context_info: Dict[str, str] = None) -> None:
    """
    Track comments that were sent to LLM but came back BENIGN.
    Auto-cleans entries older than BENIGN_TRACKING_MAX_AGE_HOURS.
    """
    comments = load_benign_analyzed()
    now = time.time()
    cutoff = now - (BENIGN_TRACKING_MAX_AGE_HOURS * 3600)
    
    # Clean old entries
    comments = [c for c in comments if c.get("timestamp", 0) > cutoff]
    
    # Don't add duplicates
    if any(c.get("comment_id") == comment_id for c in comments):
        save_benign_analyzed(comments)  # Still save to persist cleanup
        return
    
    # Extract OpenAI and Perspective scores from all_ml_scores
    openai_scores = {}
    perspective_scores = {}
    if all_ml_scores:
        for k, v in all_ml_scores.items():
            if k.startswith('openai_') and isinstance(v, (int, float)):
                openai_scores[k.replace('openai_', '')] = v
            elif k.startswith('perspective_') and isinstance(v, (int, float)):
                perspective_scores[k.replace('perspective_', '')] = v
    
    # Extract context info
    context_info = context_info or {}
    
    comments.append({
        "comment_id": comment_id,
        "permalink": permalink,
        "text": text[:500],
        "llm_reason": llm_reason,
        "detoxify_score": detoxify_score,
        "detoxify_scores": detoxify_scores,
        "openai_scores": openai_scores,
        "perspective_scores": perspective_scores,
        "is_top_level": is_top_level,
        "prefilter_trigger": prefilter_trigger,
        "post_title": context_info.get("post_title", ""),
        "parent_context": context_info.get("parent_context", "")[:500],
        "parent_author": context_info.get("parent_author", ""),
        "is_parent_op": context_info.get("is_parent_op", False),
        "grandparent_context": context_info.get("grandparent_context", "")[:300],
        "grandparent_author": context_info.get("grandparent_author", ""),
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timestamp": now
    })
    
    save_benign_analyzed(comments)
    logging.debug(f"Tracking benign analyzed comment: {comment_id}")

def track_reported_comment(comment_id: str, permalink: str, text: str, 
                           groq_reason: str, detoxify_score: float,
                           is_top_level: bool = False,
                           all_ml_scores: Dict[str, float] = None,
                           context_info: Dict[str, str] = None,
                           prefilter_trigger: str = "") -> None:
    """Add a newly reported comment to tracking"""
    comments = load_tracked_comments()
    
    # Don't add duplicates
    if any(c.get("comment_id") == comment_id for c in comments):
        return
    
    # Extract OpenAI and Perspective scores from all_ml_scores
    openai_scores = {}
    perspective_scores = {}
    detoxify_scores = {}
    prefilter_trigger_from_scores = ""
    if all_ml_scores:
        for k, v in all_ml_scores.items():
            if k.startswith('openai_') and isinstance(v, (int, float)):
                openai_scores[k.replace('openai_', '')] = v
            elif k.startswith('perspective_') and isinstance(v, (int, float)):
                perspective_scores[k.replace('perspective_', '')] = v
            elif k == '_trigger_reasons':
                prefilter_trigger_from_scores = str(v)
            elif not k.startswith('_') and isinstance(v, (int, float)):
                # Detoxify scores don't have a prefix
                detoxify_scores[k] = v
    
    # Use explicit prefilter_trigger if provided, else extract from scores
    final_trigger = prefilter_trigger or prefilter_trigger_from_scores
    
    # Extract context info
    context_info = context_info or {}
    
    comments.append({
        "comment_id": comment_id,
        "permalink": permalink,
        "text": text[:500],  # Truncate long comments
        "groq_reason": groq_reason,
        "detoxify_score": detoxify_score,
        "detoxify_scores": detoxify_scores,
        "openai_scores": openai_scores,
        "perspective_scores": perspective_scores,
        "is_top_level": is_top_level,
        "prefilter_trigger": final_trigger,
        "post_title": context_info.get("post_title", ""),
        "parent_context": context_info.get("parent_context", "")[:500],
        "parent_author": context_info.get("parent_author", ""),
        "is_parent_op": context_info.get("is_parent_op", False),
        "grandparent_context": context_info.get("grandparent_context", "")[:300],
        "grandparent_author": context_info.get("grandparent_author", ""),
        "reported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outcome": "pending",
        "checked_at": ""
    })
    
    save_tracked_comments(comments)
    logging.debug(f"Tracking reported comment: {comment_id}")

def check_reported_outcomes(reddit: praw.Reddit, min_age_hours: int = 24) -> Dict[str, int]:
    """
    Check outcomes of pending reported comments.
    Returns stats dict with counts.
    
    Outcomes:
    - removed: Comment was removed (by mod, automod, admin, or deleted)
    - approved: Comment was explicitly approved (num_reports==0 or approved_by set)
    - pending: Still in modqueue, no action taken yet
    """
    comments = load_tracked_comments()
    now = time.time()
    stats = {"checked": 0, "removed": 0, "approved": 0, "still_pending": 0, "errors": 0}
    
    for entry in comments:
        if entry.get("outcome") != "pending":
            continue
        
        # Check if comment is old enough
        reported_at = entry.get("reported_at", "")
        if reported_at:
            try:
                reported_time = time.mktime(time.strptime(reported_at, "%Y-%m-%dT%H:%M:%SZ"))
                age_hours = (now - reported_time) / 3600
                if age_hours < min_age_hours:
                    stats["still_pending"] += 1
                    continue
            except ValueError:
                pass
        
        # Check comment status via Reddit API
        comment_id = entry.get("comment_id", "")
        if not comment_id:
            continue
            
        try:
            # Remove t1_ prefix if present for fetching
            clean_id = comment_id.replace("t1_", "")
            comment = reddit.comment(clean_id)
            
            # Force fetch the comment data
            _ = comment.body
            
            # Check if removed
            # removed_by_category values: moderator, automod_filtered, deleted, author, 
            # anti_evil_ops, content_takedown, reddit
            removed_by = getattr(comment, 'removed_by_category', None)
            
            if comment.body == "[removed]" or getattr(comment, 'removed', False):
                entry["outcome"] = "removed"
                entry["removed_by"] = removed_by or "unknown"
                stats["removed"] += 1
            elif removed_by:
                entry["outcome"] = "removed"
                entry["removed_by"] = removed_by
                stats["removed"] += 1
            else:
                # Comment still exists - check for positive approval evidence
                num_reports = getattr(comment, 'num_reports', None)
                approved_by = getattr(comment, 'approved_by', None)
                
                if num_reports == 0 or approved_by is not None:
                    # Mod explicitly approved or cleared reports
                    entry["outcome"] = "approved"
                    stats["approved"] += 1
                else:
                    # Still in modqueue, no action taken
                    stats["still_pending"] += 1
                    continue  # Don't update checked_at, keep as pending
            
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["checked"] += 1
            
        except prawcore.exceptions.NotFound:
            # Comment was deleted (by user or mod)
            entry["outcome"] = "removed"
            entry["removed_by"] = "deleted_or_notfound"
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["removed"] += 1
            stats["checked"] += 1
        except Exception as e:
            logging.warning(f"Error checking comment {comment_id}: {e}")
            stats["errors"] += 1
    
    save_tracked_comments(comments)
    return stats

def cleanup_old_tracked(max_age_days: int = 30) -> int:
    """Remove entries older than max_age_days that have been resolved"""
    comments = load_tracked_comments()
    now = time.time()
    original_count = len(comments)
    
    filtered = []
    for entry in comments:
        # Keep pending entries regardless of age
        if entry.get("outcome") == "pending":
            filtered.append(entry)
            continue
        
        # Check age of resolved entries
        checked_at = entry.get("checked_at", "")
        if checked_at:
            try:
                checked_time = time.mktime(time.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ"))
                age_days = (now - checked_time) / 86400
                if age_days < max_age_days:
                    filtered.append(entry)
            except ValueError:
                filtered.append(entry)
        else:
            filtered.append(entry)
    
    save_tracked_comments(filtered)
    removed = original_count - len(filtered)
    if removed > 0:
        logging.info(f"Cleaned up {removed} old tracking entries")
    return removed

def get_accuracy_stats(hours: int = None, reddit: 'praw.Reddit' = None, save_updates: bool = True, 
                       rate_limit_delay: float = 0.0) -> Dict[str, any]:
    """
    Calculate accuracy statistics from tracked comments.
    
    Args:
        hours: If provided, only count items from the last N hours.
               If None, count all items (all-time stats).
        reddit: If provided, do live checks on pending items to get current status.
        save_updates: If True and reddit checks found updates, save them to disk.
        rate_limit_delay: Seconds to wait between Reddit API calls (0 = no delay).
    """
    all_comments = load_tracked_comments()
    
    # Filter by time if specified
    if hours is not None:
        cutoff = time.time() - (hours * 3600)
        comments = []
        for c in all_comments:
            reported_at = c.get("reported_at", "")
            if reported_at:
                try:
                    reported_time = time.mktime(time.strptime(reported_at, "%Y-%m-%dT%H:%M:%SZ"))
                    if reported_time >= cutoff:
                        comments.append(c)
                except ValueError:
                    pass  # Skip malformed timestamps
    else:
        comments = all_comments
    
    # If reddit client provided, do live checks on pending items
    updates_made = False
    if reddit is not None:
        import prawcore.exceptions
        pending_items = [c for c in comments if c.get("outcome") == "pending"]
        
        if pending_items and rate_limit_delay > 0:
            logging.info(f"Checking {len(pending_items)} pending items with {rate_limit_delay}s delay between calls...")
        
        for i, c in enumerate(pending_items):
            comment_id = c.get("comment_id", "")
            if not comment_id:
                continue
            
            # Rate limiting
            if rate_limit_delay > 0 and i > 0:
                time.sleep(rate_limit_delay)
            
            try:
                clean_id = comment_id.replace("t1_", "")
                comment = reddit.comment(clean_id)
                _ = comment.body  # Force fetch
                
                old_outcome = c.get("outcome")
                removed_by = getattr(comment, 'removed_by_category', None)
                
                if comment.body == "[removed]" or getattr(comment, 'removed', False):
                    c["outcome"] = "removed"
                    c["removed_by"] = removed_by or "unknown"
                    c["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                elif removed_by:
                    c["outcome"] = "removed"
                    c["removed_by"] = removed_by
                    c["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                else:
                    # Comment still exists and wasn't removed
                    # Only mark as approved if we have positive evidence:
                    # - num_reports == 0 means mod explicitly cleared/approved
                    # - approved_by is set means mod clicked approve
                    num_reports = getattr(comment, 'num_reports', None)
                    approved_by = getattr(comment, 'approved_by', None)
                    
                    if num_reports == 0 or approved_by is not None:
                        c["outcome"] = "approved"
                        c["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    # Otherwise keep as pending - still in modqueue
                
                if c.get("outcome") != old_outcome:
                    updates_made = True
                    
            except prawcore.exceptions.NotFound:
                c["outcome"] = "removed"  # Comment deleted/removed
                c["removed_by"] = "deleted_or_notfound"
                c["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                updates_made = True
            except Exception:
                pass  # Keep as pending on error
        
        if pending_items and rate_limit_delay > 0:
            logging.info(f"Finished checking {len(pending_items)} pending items")
    
    # Save updates back to disk if any were made
    if updates_made and save_updates:
        save_tracked_comments(all_comments)
    
    total = len(comments)
    pending = sum(1 for c in comments if c.get("outcome") == "pending")
    removed = sum(1 for c in comments if c.get("outcome") == "removed")
    approved = sum(1 for c in comments if c.get("outcome") == "approved")
    
    resolved = removed + approved
    accuracy = (removed / resolved * 100) if resolved > 0 else 0
    
    return {
        "total_tracked": total,
        "pending": pending,
        "removed": removed,
        "approved": approved,
        "resolved": resolved,
        "accuracy_pct": accuracy
    }


# -------------------------------
# Config
# -------------------------------

@dataclass
class Config:
    # Reddit
    client_id: str
    client_secret: str
    username: str
    password: str
    user_agent: str
    subreddits: List[str]

    # LLM
    groq_api_key: str
    groq_reasoning_effort: str  # "low", "medium", or "high" for Groq reasoning models
    xai_api_key: str  # Optional: for x.ai Grok models
    xai_reasoning_effort: str  # "low", "medium", or "high" for Grok reasoning
    llm_model: str
    llm_fallback_chain: List[str]  # Fallback models in order of preference
    llm_daily_limit: int        # Switch to fallback after this many calls
    llm_requests_per_minute: int  # Max requests per minute to Groq
    
    # Detoxify pre-filter
    detoxify_model: str        # "original" or "unbiased"
    detoxify_can_escalate: bool  # Whether Detoxify can trigger LLM review on its own
    
    # OpenAI Moderation API (optional, free supplement to Detoxify)
    openai_moderation_key: str      # API key for OpenAI (also used for moderation)
    openai_moderation_enabled: bool # Whether to use OpenAI Moderation API
    openai_moderation_threshold: float  # Base threshold (0.0-1.0)
    openai_moderation_rpm: int      # Rate limit (requests per minute)
    openai_moderation_mode: str     # "all" (every comment), "confirm" (only if Detoxify triggers), "only" (no Detoxify)
    
    # Perspective API (optional, free from Google)
    perspective_api_key: str        # API key from Google Cloud
    perspective_enabled: bool       # Whether to use Perspective API
    perspective_threshold: float    # Base threshold (0.0-1.0)
    perspective_rpm: int            # Rate limit (requests per minute)
    perspective_mode: str           # "all" (every comment), "confirm" (only if Detoxify triggers), "only" (no Detoxify)
    
    # Detoxify thresholds per label
    threshold_threat: float
    threshold_severe_toxicity: float
    threshold_identity_attack: float
    threshold_insult_directed: float
    threshold_insult_not_directed: float
    threshold_toxicity_directed: float
    threshold_toxicity_not_directed: float
    threshold_obscene: float
    threshold_borderline: float  # Score above this logs as borderline skip
    
    # Auto-remove settings
    auto_remove_enabled: bool       # Master switch for auto-remove
    auto_remove_require_models: List[str]  # Which models must agree (detoxify, openai, perspective)
    auto_remove_min_consensus: int  # Minimum number of models that must agree
    auto_remove_detoxify_min: float # Min Detoxify score for auto-remove
    auto_remove_openai_min: float   # Min OpenAI score for auto-remove
    auto_remove_perspective_min: float  # Min Perspective score for auto-remove
    auto_remove_on_pattern_match: bool  # Auto-remove on pattern matches (slurs, threats)
    
    # Custom moderation guidelines (loaded from file or env)
    moderation_guidelines: str

    # Reporting behavior
    report_as: str              # "moderator" | "user"
    report_rule_bucket: str
    enable_reddit_reports: bool
    dry_run: bool

    # Discord
    enable_discord: bool
    discord_webhook: str
    
    # Discord Bot (for editable review notifications)
    discord_bot_token: str          # Bot token for posting/editing messages
    discord_review_channel_id: str  # Channel ID for review notifications
    discord_review_check_interval: int  # How often to check for reviewed comments (seconds)

    # Runtime
    log_level: str


def load_config() -> Config:
    """Load and validate configuration from environment"""
    
    # Required Reddit vars
    for key in [
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_USER_AGENT",
    ]:
        if not os.getenv(key):
            raise KeyError(f"Missing required env var: {key}")

    # Required Groq key
    if not os.getenv("GROQ_API_KEY"):
        raise KeyError("Missing required env var: GROQ_API_KEY (get free key at console.groq.com)")

    subs = os.getenv("SUBREDDITS", "").strip()
    if not subs:
        raise KeyError("SUBREDDITS is required, e.g. 'UFOs' or 'a,b,c'")

    # Load moderation guidelines from file or env
    guidelines_path = os.getenv("MODERATION_GUIDELINES_FILE", "moderation_guidelines.txt")
    guidelines = os.getenv("MODERATION_GUIDELINES", "").strip()
    
    if not guidelines and os.path.exists(guidelines_path):
        with open(guidelines_path, "r", encoding="utf-8") as f:
            guidelines = f.read().strip()
    
    if not guidelines:
        raise KeyError(f"No moderation guidelines found. Create '{guidelines_path}' or set MODERATION_GUIDELINES env var.")

    cfg = Config(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
        subreddits=[s.strip() for s in subs.split(",") if s.strip()],
        
        groq_api_key=os.environ["GROQ_API_KEY"],
        groq_reasoning_effort=os.getenv("GROQ_REASONING_EFFORT", "medium"),  # "low", "medium", or "high"
        xai_api_key=os.getenv("XAI_API_KEY", ""),  # Optional
        xai_reasoning_effort=os.getenv("XAI_REASONING_EFFORT", "low"),  # "low", "medium", or "high"
        llm_model=os.getenv("LLM_MODEL", "groq/compound"),
        llm_fallback_chain=[s.strip() for s in os.getenv("LLM_FALLBACK_CHAIN", 
            "llama-3.3-70b-versatile,meta-llama/llama-4-scout-17b-16e-instruct,meta-llama/llama-4-maverick-17b-128e-instruct,llama-3.1-8b-instant"
        ).split(",") if s.strip()],
        llm_daily_limit=int(os.getenv("LLM_DAILY_LIMIT", "240")),
        llm_requests_per_minute=int(os.getenv("LLM_REQUESTS_PER_MINUTE", "2")),
        
        detoxify_model=os.getenv("DETOXIFY_MODEL", "original"),
        detoxify_can_escalate=os.getenv("DETOXIFY_CAN_ESCALATE", "true").lower() == "true",
        
        # OpenAI Moderation API settings
        openai_moderation_key=os.getenv("OPENAI_API_KEY", ""),  # Reuse same key as for other OpenAI
        openai_moderation_enabled=os.getenv("OPENAI_MODERATION_ENABLED", "false").lower() == "true",
        openai_moderation_threshold=float(os.getenv("OPENAI_MODERATION_THRESHOLD", "0.50")),
        openai_moderation_rpm=int(os.getenv("OPENAI_MODERATION_RPM", "30")),  # Requests per minute
        openai_moderation_mode=os.getenv("OPENAI_MODERATION_MODE", "confirm"),  # "all", "confirm", or "only"
        
        # Perspective API settings
        perspective_api_key=os.getenv("PERSPECTIVE_API_KEY", ""),
        perspective_enabled=os.getenv("PERSPECTIVE_ENABLED", "false").lower() == "true",
        perspective_threshold=float(os.getenv("PERSPECTIVE_THRESHOLD", "0.70")),
        perspective_rpm=int(os.getenv("PERSPECTIVE_RPM", "60")),  # Requests per minute
        perspective_mode=os.getenv("PERSPECTIVE_MODE", "confirm"),  # "all", "confirm", or "only"
        
        # Per-label thresholds (lower = more sensitive)
        threshold_threat=float(os.getenv("THRESHOLD_THREAT", "0.15")),
        threshold_severe_toxicity=float(os.getenv("THRESHOLD_SEVERE_TOXICITY", "0.20")),
        threshold_identity_attack=float(os.getenv("THRESHOLD_IDENTITY_ATTACK", "0.25")),
        threshold_insult_directed=float(os.getenv("THRESHOLD_INSULT_DIRECTED", "0.40")),
        threshold_insult_not_directed=float(os.getenv("THRESHOLD_INSULT_NOT_DIRECTED", "0.60")),
        threshold_toxicity_directed=float(os.getenv("THRESHOLD_TOXICITY_DIRECTED", "0.40")),
        threshold_toxicity_not_directed=float(os.getenv("THRESHOLD_TOXICITY_NOT_DIRECTED", "0.50")),
        threshold_obscene=float(os.getenv("THRESHOLD_OBSCENE", "0.90")),
        threshold_borderline=float(os.getenv("THRESHOLD_BORDERLINE", "0.35")),
        
        # Auto-remove settings
        auto_remove_enabled=os.getenv("AUTO_REMOVE_ENABLED", "false").lower() == "true",
        auto_remove_require_models=[s.strip().lower() for s in os.getenv("AUTO_REMOVE_REQUIRE_MODELS", "openai,perspective").split(",") if s.strip()],
        auto_remove_min_consensus=int(os.getenv("AUTO_REMOVE_MIN_CONSENSUS", "2")),
        auto_remove_detoxify_min=float(os.getenv("AUTO_REMOVE_DETOXIFY_MIN", "0.70")),
        auto_remove_openai_min=float(os.getenv("AUTO_REMOVE_OPENAI_MIN", "0.70")),
        auto_remove_perspective_min=float(os.getenv("AUTO_REMOVE_PERSPECTIVE_MIN", "0.70")),
        auto_remove_on_pattern_match=os.getenv("AUTO_REMOVE_ON_PATTERN_MATCH", "false").lower() == "true",
        
        moderation_guidelines=guidelines,
        
        report_as=os.getenv("REPORT_AS", "moderator").lower(),
        report_rule_bucket=os.getenv("REPORT_RULE_BUCKET", "").strip(),
        enable_reddit_reports=os.getenv("ENABLE_REDDIT_REPORTS", "true").lower() == "true",
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        
        enable_discord=os.getenv("ENABLE_DISCORD", "false").lower() == "true",
        discord_webhook=os.getenv("DISCORD_WEBHOOK", "").strip(),
        
        # Discord Bot settings
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        discord_review_channel_id=os.getenv("DISCORD_REVIEW_CHANNEL_ID", "").strip(),
        discord_review_check_interval=int(os.getenv("DISCORD_REVIEW_CHECK_INTERVAL", "120")),  # 2 minutes default
        
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    return cfg


# -------------------------------
# Logging
# -------------------------------

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


# -------------------------------
# Reddit
# -------------------------------

def praw_client(cfg: Config) -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        username=cfg.username,
        password=cfg.password,
        user_agent=cfg.user_agent,
        ratelimit_seconds=60,
    )
    me = reddit.user.me()
    logging.info(f"Authenticated as u/{me.name}")
    return reddit


# -------------------------------
# Pre-filter using Detoxify
# -------------------------------

import re

# ============================================
# LOAD PATTERNS FROM JSON
# ============================================

PATTERNS_FILE = "moderation_patterns.json"

def load_moderation_patterns(path: str = PATTERNS_FILE) -> Dict:
    """Load moderation patterns from JSON file"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.warning(f"Patterns file not found at {path}, using defaults")
        return {}
    except json.JSONDecodeError as e:
        logging.warning(f"Could not parse {path}: {e}, using defaults")
        return {}

# Load patterns at module level
PATTERNS = load_moderation_patterns()

# ============================================
# 1. TEXT NORMALIZATION & DE-OBFUSCATION
# ============================================

# Get leet map from JSON or use defaults
LEET_MAP = PATTERNS.get("obfuscation_map", {}).get("leet_speak", {
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's',
    '7': 't', '8': 'b', '@': 'a', '$': 's', '!': 'i',
})

# Get common evasions from JSON
COMMON_EVASIONS = PATTERNS.get("obfuscation_map", {}).get("common_evasions", {
    "ph": "f", "ck": "k"
})

def normalize_text(text: str) -> str:
    """
    Normalize text for pattern matching.
    Returns lowercase with common obfuscations removed.
    """
    result = text.lower()
    
    # Replace leet speak
    for leet, normal in LEET_MAP.items():
        result = result.replace(leet, normal)
    
    # Apply common evasions
    for evasion, replacement in COMMON_EVASIONS.items():
        result = result.replace(evasion, replacement)
    
    return result

def squash_text(text: str) -> str:
    """
    Remove spaces and punctuation for catching spaced-out evasions.
    "k y s" -> "kys", "s.h" -> "sh"
    """
    result = normalize_text(text)
    result = re.sub(r'[^a-z0-9]', '', result)
    return result


# ============================================
# 2. BUILD PATTERN LISTS FROM JSON
# ============================================

def build_slur_sets() -> Tuple[set, set]:
    """
    Build separate sets for single-word slurs and multi-word slur phrases.
    Returns (slur_words, slur_phrases)
    """
    slur_words = set()
    slur_phrases = set()
    slurs_data = PATTERNS.get("slurs", {})
    for category, words in slurs_data.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            for w in words:
                w_lower = w.lower()
                if ' ' in w_lower:
                    slur_phrases.add(w_lower)
                else:
                    slur_words.add(w_lower)
    return slur_words, slur_phrases

def build_self_harm_set() -> set:
    """Build set of self-harm phrases from JSON"""
    phrases = set()
    self_harm = PATTERNS.get("self_harm", {}).get("phrases", [])
    phrases.update(p.lower() for p in self_harm)
    return phrases

def build_threat_set() -> set:
    """Build set of threat phrases from JSON"""
    phrases = set()
    threats = PATTERNS.get("threats", {})
    for category, words in threats.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            phrases.update(w.lower() for w in words)
    return phrases

def build_sexual_violence_set() -> set:
    """Build set of sexual violence phrases from JSON"""
    phrases = PATTERNS.get("sexual_violence", {}).get("phrases", [])
    return set(p.lower() for p in phrases)

def build_brigading_set() -> set:
    """Build set of brigading/harassment phrases from JSON"""
    phrases = PATTERNS.get("brigading_harassment", {}).get("phrases", [])
    return set(p.lower() for p in phrases)

def build_shill_set() -> set:
    """Build set of shill accusation phrases from JSON"""
    terms = PATTERNS.get("shill_accusations", {}).get("terms", [])
    return set(t.lower() for t in terms)

def build_dismissive_hostile_sets() -> Tuple[set, set, set]:
    """
    Build sets of dismissive/hostile phrases from JSON.
    Returns (hard_phrases, soft_phrases, gatekeeping_phrases)
    - Hard: Always escalate on reply (fuck off, eat shit, etc.)
    - Soft: Only escalate when strongly directed (cope, touch grass, etc.)
    - Gatekeeping: "please don't post again", "delete your account", etc.
    """
    dismissive = PATTERNS.get("dismissive_hostile", {})
    hard = set(p.lower() for p in dismissive.get("hard", []))
    soft = set(p.lower() for p in dismissive.get("soft", []))
    gatekeeping = set(p.lower() for p in dismissive.get("gatekeeping", []))
    # Fallback for old format
    if not hard and not soft:
        phrases = dismissive.get("phrases", [])
        hard = set(p.lower() for p in phrases)
    return hard, soft, gatekeeping

def build_insult_sets() -> Tuple[set, set]:
    """
    Build sets for direct insults from JSON.
    Returns (insult_words, insult_phrases)
    """
    insult_words = set()
    insult_phrases = set()
    
    # Load from insults_direct
    insults_data = PATTERNS.get("insults_direct", {})
    for category, words in insults_data.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            for w in words:
                w_lower = w.lower()
                if ' ' in w_lower:
                    insult_phrases.add(w_lower)
                else:
                    insult_words.add(w_lower)
    
    # Also load from profanity_insults (vulgar insults, crude anatomical)
    profanity_data = PATTERNS.get("profanity_insults", {})
    for category, words in profanity_data.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            for w in words:
                w_lower = w.lower()
                if ' ' in w_lower:
                    insult_phrases.add(w_lower)
                else:
                    insult_words.add(w_lower)
    
    return insult_words, insult_phrases

def build_benign_phrases_set() -> set:
    """Build set of benign skip phrases from JSON - PHRASES ONLY, not single words"""
    phrases = set()
    benign = PATTERNS.get("benign_skip", {})
    for category, words in benign.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            # Only add multi-word phrases
            for phrase in words:
                if ' ' in phrase:  # Must be a phrase, not a single word
                    phrases.add(phrase.lower())
    return phrases

def build_violence_illegal_set() -> set:
    """Build set of violence/illegal advocacy phrases from JSON"""
    phrases = PATTERNS.get("violence_illegal_advocacy", {}).get("phrases", [])
    return set(p.lower() for p in phrases)

def build_contextual_terms_sets() -> Tuple[set, set]:
    """
    Build sets of contextual sensitive terms from JSON.
    These are ambiguous terms that should only escalate with additional signals.
    Returns (context_words, context_phrases)
    """
    context_words = set()
    context_phrases = set()
    contextual = PATTERNS.get("contextual_sensitive_terms", {})
    for category, words in contextual.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            for w in words:
                w_lower = w.lower()
                if ' ' in w_lower:
                    context_phrases.add(w_lower)
                else:
                    context_words.add(w_lower)
    return context_words, context_phrases

def build_accusations_set() -> set:
    """Build set of bad faith accusation phrases from JSON"""
    phrases = set()
    accusations = PATTERNS.get("accusations", {})
    for category, words in accusations.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            phrases.update(w.lower() for w in words)
    return phrases

def build_harassment_sets() -> Tuple[set, set, set]:
    """
    Build sets of harassment phrases from JSON.
    Returns (mod_accusations, condescension_mockery, emoji_mockery)
    """
    harassment = PATTERNS.get("harassment", {})
    mod_accusations = set(p.lower() for p in harassment.get("mod_accusations", []))
    condescension = set(p.lower() for p in harassment.get("condescension_mockery", []))
    emoji = set(p for p in harassment.get("emoji_mockery", []))  # Don't lowercase emojis
    return mod_accusations, condescension, emoji

def build_slur_exceptions_set() -> set:
    """Build set of phrases that contain slurs but are benign (e.g., 'go poof')"""
    exceptions = PATTERNS.get("benign_skip", {}).get("slur_exceptions", [])
    return set(p.lower() for p in exceptions)

def build_vote_manipulation_set() -> set:
    """Build set of vote manipulation accusation phrases from JSON"""
    phrases = PATTERNS.get("shill_accusations", {}).get("vote_manipulation", [])
    return set(p.lower() for p in phrases)

def build_dehumanizing_set() -> Tuple[set, set]:
    """
    Build sets for dehumanizing insults from JSON.
    Returns (dehumanizing_words, dehumanizing_phrases)
    """
    words = set()
    phrases = set()
    dehumanizing = PATTERNS.get("insults_direct", {}).get("dehumanizing", [])
    for item in dehumanizing:
        item_lower = item.lower()
        if ' ' in item_lower:
            phrases.add(item_lower)
        else:
            words.add(item_lower)
    return words, phrases

def build_veiled_threats_set() -> set:
    """Build set of veiled threat/omen phrases from JSON"""
    phrases = PATTERNS.get("threats", {}).get("veiled_omen", [])
    return set(p.lower() for p in phrases)

def build_homophobic_pejorative_set() -> set:
    """Build set of homophobic pejorative phrases (gay used as insult)"""
    phrases = PATTERNS.get("contextual_sensitive_terms", {}).get("homophobic_pejorative", [])
    return set(p.lower() for p in phrases)

# Build sets at module level
SLUR_WORDS, SLUR_PHRASES = build_slur_sets()
SLUR_EXCEPTIONS = build_slur_exceptions_set()
SELF_HARM_PHRASES = build_self_harm_set()
THREAT_PHRASES = build_threat_set()
SEXUAL_VIOLENCE_PHRASES = build_sexual_violence_set()
BRIGADING_PHRASES = build_brigading_set()
SHILL_PHRASES = build_shill_set()
DISMISSIVE_HARD_PHRASES, DISMISSIVE_SOFT_PHRASES, DISMISSIVE_GATEKEEPING_PHRASES = build_dismissive_hostile_sets()
INSULT_WORDS, INSULT_PHRASES = build_insult_sets()
VIOLENCE_ILLEGAL_PHRASES = build_violence_illegal_set()
CONTEXTUAL_WORDS, CONTEXTUAL_PHRASES = build_contextual_terms_sets()
BENIGN_PHRASES_SET = build_benign_phrases_set()
ACCUSATION_PHRASES = build_accusations_set()
HARASSMENT_MOD_PHRASES, HARASSMENT_CONDESCENSION_PHRASES, HARASSMENT_EMOJI = build_harassment_sets()
VOTE_MANIPULATION_PHRASES = build_vote_manipulation_set()
DEHUMANIZING_WORDS, DEHUMANIZING_PHRASES = build_dehumanizing_set()
VEILED_THREAT_PHRASES = build_veiled_threats_set()
HOMOPHOBIC_PEJORATIVE_PHRASES = build_homophobic_pejorative_set()

# Note: Pattern counts are logged when SmartPreFilter initializes (after logging is configured)

# ============================================
# 3. MUST-ESCALATE REGEX PATTERNS
# ============================================

def build_must_escalate_regex() -> List[re.Pattern]:
    """Build compiled regex patterns from JSON"""
    patterns = PATTERNS.get("regex_patterns", {}).get("must_escalate", [])
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logging.warning(f"Invalid regex pattern '{p}': {e}")
    return compiled

MUST_ESCALATE_RE = build_must_escalate_regex()

# Benign phrase regex - allow trailing lol/lmao/emoji/punctuation
# These match common exclamations that are clearly not attacks
BENIGN_TAIL_PATTERN = r'[\s.,!?…]*(?:lol|lmao|rofl|haha|😂|🤣|😭|💀|🔥|👀|😱|🤯|omg|bruh)?[\s.,!?…😂🤣😭💀🔥👀😱🤯]*$'

BENIGN_PHRASES_RE = [
    re.compile(r'^(holy\s+)?(shit|fuck|crap|hell|cow)' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
    re.compile(r'^what\s+the\s+(fuck|hell|heck)' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
    re.compile(r'^(oh\s+)?(my\s+)?(god|gosh|lord)' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
    re.compile(r'^(damn|dang|darn)' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
    re.compile(r'^no\s+(fucking|freaking)?\s*way' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
    re.compile(r'^(wow|whoa|woah)' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
    re.compile(r'^(omg|wtf|lol|lmao|bruh)' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
    re.compile(r'^(this is |that\'?s )?(insane|crazy|wild|nuts|unreal|incredible|amazing)' + BENIGN_TAIL_PATTERN, re.IGNORECASE),
]


# ============================================
# 4. DIRECTEDNESS CHECK
# ============================================

def is_strongly_directed(text: str) -> bool:
    """
    Check if comment is STRONGLY directed at another user.
    Use this for threshold lowering and shill accusation logic.
    
    Strong signals: explicit user reference, "you/your", "OP", "mods",
    collective addresses like "y'all", "you guys", "everyone here", "this sub",
    imperatives like "quit being", "stop being" (commands directed at reader)
    
    Excludes "generic you" phrases like "you don't need to", "if you think", etc.
    which are impersonal and not directed at a specific user.
    """
    text_lower = text.lower()
    
    # Explicit user mention - always directed
    if re.search(r'\bu/\w+', text_lower):
        return True
    
    # Check for "you/your" words
    has_you = bool(re.search(r'\b(you|your|you\'re|youre|ur)\b', text_lower))
    
    if has_you:
        # Check if ALL instances of "you" are in generic phrases
        generic_phrases = PATTERNS.get("regex_patterns", {}).get("generic_you_phrases", [])
        
        # If any generic phrase is found, check if "you" appears outside of it
        text_check = text_lower
        for phrase in generic_phrases:
            phrase_lower = phrase.lower()
            # For phrases ending in punctuation, use substring match
            # For phrases ending in word characters, use word boundary
            if phrase_lower[-1].isalnum():
                pattern = r'\b' + re.escape(phrase_lower) + r'\b'
                text_check = re.sub(pattern, '', text_check)
            else:
                # Substring replacement for phrases ending in punctuation
                text_check = text_check.replace(phrase_lower, '')
        
        # If "you" still appears after removing generic phrases, it's directed
        if re.search(r'\b(you|your|you\'re|youre|ur)\b', text_check):
            return True
        else:
            # All "you" instances were in generic phrases - not directed
            has_you = False
    
    if has_you:
        return True
    
    # OP reference
    if re.search(r'\bop\b', text_lower):
        return True
    # Mod reference (often targeted)
    if re.search(r'\bmods?\b', text_lower):
        return True
    # Y'all / yall
    if re.search(r'\by\'?all\b', text_lower):
        return True
    # Collective: "you all", "you guys", "you people"
    if re.search(r'\byou (all|guys|people)\b', text_lower):
        return True
    # "all of you"
    if re.search(r'\ball of you\b', text_lower):
        return True
    # "everyone here"
    if re.search(r'\beveryone here\b', text_lower):
        return True
    # "people here" (attacking users in this sub)
    if re.search(r'\bpeople here\b', text_lower):
        return True
    # "this sub" / "this subreddit" (attacking the community)
    if re.search(r'\bthis (sub|subreddit)\b', text_lower):
        return True
    
    # Direct address terms combined with negative content
    # "bro", "dude", "man" when used to address someone directly
    # Only count as directed if followed by criticism/insult patterns
    if re.search(r'\b(come on|shut up|wtf|calm down|chill out)\s*(bro|dude|man)\b', text_lower):
        return True
    if re.search(r'\b(bro|dude|man)\s*,?\s*(this is|you\'re|you are|that\'s)\s*(stupid|dumb|idiotic|moronic|ridiculous)', text_lower):
        return True
    
    # Imperatives - commands directed at the reader even without "you"
    # "quit being stupid", "stop being dumb", "don't be an idiot"
    if re.search(r'\b(quit|stop)\s+being?\s+', text_lower):
        return True
    # "don't be", "never be" - also imperatives
    if re.search(r'\b(don\'t|dont|never)\s+be\s+', text_lower):
        return True
    # "go away", "get lost", "get out" - commands
    if re.search(r'\b(go|get)\s+(away|lost|out|fucked)\b', text_lower):
        return True
    
    return False

def is_weakly_directed(text: str) -> bool:
    """
    Check for weak directedness signals.
    "this guy", "this dude", etc. - often refers to public figures, not users.
    """
    text_lower = text.lower()
    if re.search(r'\b(this\s+)?(guy|dude|person)\b', text_lower):
        return True
    return False

# For backwards compatibility, keep is_directed_at_person as alias for strong
def is_directed_at_person(text: str) -> bool:
    """Alias for is_strongly_directed"""
    return is_strongly_directed(text)

def get_non_quoted_text(text: str) -> str:
    """
    Extract the non-quoted portion of a comment.
    Reddit quotes start with '>' at the beginning of a line.
    Returns the text without quoted lines.
    """
    lines = text.split('\n')
    non_quoted = []
    for line in lines:
        stripped = line.strip()
        # Skip lines that start with > (quotes)
        if not stripped.startswith('>'):
            non_quoted.append(line)
    return '\n'.join(non_quoted).strip()

def is_primarily_quote(text: str) -> bool:
    """
    Check if a comment is primarily quoting someone else.
    Returns True if more than 50% of the content is quoted.
    """
    lines = text.split('\n')
    quoted_chars = 0
    total_chars = 0
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        total_chars += len(stripped)
        if stripped.startswith('>'):
            quoted_chars += len(stripped)
    
    if total_chars == 0:
        return False
    
    return (quoted_chars / total_chars) > 0.5


# ============================================
# 5. PHRASE MATCHING HELPERS
# ============================================

def contains_slur(text: str) -> bool:
    """
    Check if text contains any slur words OR slur phrases.
    Handles both single-word slurs (token match) and multi-word slurs (substring match).
    Checks for benign exception phrases first (e.g., "go poof" is not a slur).
    """
    normalized = normalize_text(text)
    
    # First check if any slur exception phrases are present
    # If so, the slur is being used in a benign context
    for exception in SLUR_EXCEPTIONS:
        if exception in normalized:
            # This slur usage is benign (e.g., "go poof" meaning vanish)
            return False
    
    # Check single-word slurs via tokenization
    words = set(re.findall(r'\b\w+\b', normalized))
    if words & SLUR_WORDS:
        return True
    
    # Check multi-word slur phrases with word boundaries
    for phrase in SLUR_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def contains_self_harm(text: str) -> bool:
    """Check if text contains self-harm encouragement"""
    normalized = normalize_text(text)
    squashed = squash_text(text)
    
    # Check squashed for spaced evasions like "k y s" or "k.y" but NOT words that 
    # happen to contain these letters (e.g., "sticky slots" -> "stickys" contains "kys")
    # Only match if the squashed pattern appears near word boundaries in original
    
    # For "kys" - only match if original has k, y, s separated by non-letters
    # e.g., "k y s", "k.y.s", "k-y-s" but not "stickys"
    kys_pattern = r'\bk[\s\.\-\_\*]*y[\s\.\-\_\*]*s\b'
    if re.search(kys_pattern, normalized, re.IGNORECASE):
        return True
    
    # For "kill yourself" with spaces/punctuation
    if 'killyourself' in squashed:
        # Verify it's actually spaced out, not part of another word
        kill_yourself_pattern = r'\bkill[\s\.\-\_\*]*your[\s\.\-\_\*]*self\b'
        if re.search(kill_yourself_pattern, normalized, re.IGNORECASE):
            return True
    
    # "go die" with spaces  
    if 'godie' in squashed:
        go_die_pattern = r'\bgo[\s\.\-\_\*]*die\b'
        if re.search(go_die_pattern, normalized, re.IGNORECASE):
            return True
            
    # "drink bleach" with spaces
    if 'drinkbleach' in squashed:
        drink_bleach_pattern = r'\bdrink[\s\.\-\_\*]*bleach\b'
        if re.search(drink_bleach_pattern, normalized, re.IGNORECASE):
            return True
    
    # Check phrases with word boundaries to avoid false matches
    # e.g., "end it" should not match "recommend it"
    for phrase in SELF_HARM_PHRASES:
        # Use word boundaries for matching
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def contains_threat(text: str) -> bool:
    """Check if text contains threats"""
    normalized = normalize_text(text)
    for phrase in THREAT_PHRASES:
        # Use word boundaries to avoid false matches
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_sexual_violence(text: str) -> bool:
    """Check if text contains sexual violence threats"""
    normalized = normalize_text(text)
    for phrase in SEXUAL_VIOLENCE_PHRASES:
        # Use word boundaries to avoid false matches
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_brigading(text: str) -> bool:
    """
    Check if text contains brigading/harassment calls WITH targeting context.
    "mass report" and "everyone report" are only brigading if they target a user/person.
    Otherwise they could be "report to MUFON", "report to Congress", etc.
    """
    normalized = normalize_text(text)
    
    # Phrases that always indicate brigading (inherently targeted)
    always_brigading = {
        'everyone go harass', 'go harass this guy', 'go after this guy',
        'ruin their life', 'make them regret', 'teach them a lesson',
        'dox them', 'doxx them', 'raid this'
    }
    
    # Phrases that need targeting context
    needs_context = {'mass report', 'everyone report', 'brigade'}
    
    # Targeting indicators (user/person references)
    targeting_patterns = [
        r'\bu/', r'\bthis\s+(guy|dude|user|person|account)\b',
        r'\bthat\s+(guy|dude|user|person|account)\b',
        r'\btheir\s+(account|profile|post)\b', r'\bthis\s+post\b',
        r'\bthe\s+mods?\b', r'\bop\b', r'\bhim\b', r'\bher\b', r'\bthem\b'
    ]
    
    for phrase in BRIGADING_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            # Always-brigading phrases trigger immediately
            if phrase in always_brigading:
                return True
            
            # Context-dependent phrases need targeting
            if phrase in needs_context:
                has_targeting = any(re.search(t, normalized) for t in targeting_patterns)
                if has_targeting:
                    return True
                # Without targeting, skip (could be "report to authorities")
                continue
            
            # Other brigading phrases - trigger
            return True
    
    return False

def contains_shill_accusation(text: str) -> bool:
    """Check if text contains shill/bot accusations"""
    normalized = normalize_text(text)
    for phrase in SHILL_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_dismissive_hostile(text: str) -> Tuple[bool, str]:
    """
    Check if text contains dismissive/hostile phrases.
    Returns (matched, type) where type is 'hard', 'soft', 'gatekeeping', or '' if no match.
    - Hard: Always escalate on reply (fuck off, stfu, etc.)
    - Soft: Only escalate when strongly directed (cope, touch grass, etc.)
    - Gatekeeping: "please don't post again", "delete your account", etc.
    """
    normalized = normalize_text(text)
    
    # Check hard phrases first
    for phrase in DISMISSIVE_HARD_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True, "hard"
    
    # Check gatekeeping phrases (treat similar to hard)
    for phrase in DISMISSIVE_GATEKEEPING_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True, "gatekeeping"
    
    # Check soft phrases
    for phrase in DISMISSIVE_SOFT_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True, "soft"
    
    return False, ""

def contains_accusation(text: str) -> bool:
    """Check if text contains bad faith accusation phrases (e.g., 'you're lying')"""
    normalized = normalize_text(text)
    for phrase in ACCUSATION_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_harassment(text: str) -> Tuple[bool, str]:
    """
    Check if text contains harassment patterns.
    Returns (matched, type) where type is 'mod_accusation', 'condescension', 'emoji', or ''.
    """
    normalized = normalize_text(text)
    
    # Check mod accusations
    for phrase in HARASSMENT_MOD_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True, "mod_accusation"
    
    # Check condescension/mockery
    for phrase in HARASSMENT_CONDESCENSION_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True, "condescension"
    
    # Check emoji mockery (check original text, not normalized)
    for emoji in HARASSMENT_EMOJI:
        if emoji in text:
            return True, "emoji"
    
    return False, ""

def contains_vote_manipulation(text: str) -> bool:
    """Check if text contains vote manipulation accusations"""
    normalized = normalize_text(text)
    for phrase in VOTE_MANIPULATION_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_dehumanizing(text: str) -> bool:
    """
    Check if text contains dehumanizing insults (words or phrases).
    E.g., "cancer", "parasite", "subhuman", "waste of oxygen"
    """
    normalized = normalize_text(text)
    
    # Check single-word dehumanizing terms
    words = set(re.findall(r'\b\w+\b', normalized))
    if words & DEHUMANIZING_WORDS:
        return True
    
    # Check dehumanizing phrases with word boundaries
    for phrase in DEHUMANIZING_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def contains_veiled_threat(text: str) -> bool:
    """
    Check if text contains veiled threat/omen patterns.
    E.g., "reap the consequences", "you'll pay", "watch your back"
    """
    normalized = normalize_text(text)
    for phrase in VEILED_THREAT_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_homophobic_pejorative(text: str) -> bool:
    """
    Check if text contains homophobic pejorative usage.
    E.g., "fake and gay", "gayest shit", "that's gay"
    These are uses of 'gay' as an insult, not identity references.
    """
    normalized = normalize_text(text)
    for phrase in HOMOPHOBIC_PEJORATIVE_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_violence_illegal(text: str) -> bool:
    """
    Check if text contains violence/illegal advocacy phrases WITH exhortative context.
    Requires words like "should/let's/gonna/going to" to trigger.
    Excludes negations like "don't/never/shouldn't" which are discussions, not advocacy.
    """
    normalized = normalize_text(text)
    
    # Negation patterns - if present, this is likely discussion, not advocacy
    negation_patterns = [
        r'\bdon\'?t\b', r'\bdo\s+not\b', r'\bnever\b', r'\bshouldn\'?t\b',
        r'\bshould\s+not\b', r'\bwouldn\'?t\b', r'\bwould\s+not\b',
        r'\bcan\'?t\b', r'\bcannot\b', r'\billegal\s+to\b', r'\bagainst\s+the\s+law\b'
    ]
    
    # Exhortative patterns - advocacy requires these
    exhortative_patterns = [
        r'\bshould\b', r'\blet\'?s\b', r'\bgonna\b', r'\bgoing\s+to\b',
        r'\bneed\s+to\b', r'\bwant\s+to\b', r'\bwanna\b', r'\bgotta\b',
        r'\bwe\s+could\b', r'\bsomeone\s+should\b', r'\bwould\s+be\s+funny\b',
        r'\bi\'?m\s+gonna\b', r'\bi\'?ll\b', r'\bwe\'?ll\b', r'\bjust\b'
    ]
    
    for phrase in VIOLENCE_ILLEGAL_PHRASES:
        # Use word boundaries for all phrases to avoid false matches
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            # Check for negation first - if negated, it's discussion not advocacy
            has_negation = any(re.search(neg, normalized) for neg in negation_patterns)
            if has_negation:
                continue  # Skip - this is "don't shoot" not "shoot it"
            
            # Check for exhortative context
            has_exhortative = any(re.search(exh, normalized) for exh in exhortative_patterns)
            if has_exhortative:
                return True
            
            # Also trigger if it's a direct imperative (starts with verb)
            # e.g., "Shoot it down!" at the start
            if normalized.strip().startswith(phrase):
                return True
    
    return False

def contains_direct_insult(text: str) -> bool:
    """
    Check if text contains direct insults (words or phrases).
    Note: This should be combined with directedness check.
    """
    normalized = normalize_text(text)
    
    # Check single-word insults
    words = set(re.findall(r'\b\w+\b', normalized))
    if words & INSULT_WORDS:
        return True
    
    # Check insult phrases with word boundaries
    for phrase in INSULT_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def contains_contextual_term(text: str) -> bool:
    """
    Check if text contains contextual sensitive terms (words or phrases).
    These are ambiguous terms that need additional signals to escalate.
    """
    normalized = normalize_text(text)
    
    # Check single-word contextual terms
    words = set(re.findall(r'\b\w+\b', normalized))
    if words & CONTEXTUAL_WORDS:
        return True
    
    # Check multi-word contextual phrases with word boundaries
    for phrase in CONTEXTUAL_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def matches_any_benign_pattern(text: str) -> bool:
    """
    Check if text matches ANY benign_skip pattern from the patterns file.
    This includes:
    - self_inclusive_criticism ("we humans are stupid")
    - profanity_as_emphasis ("it's a fucking plane")
    - third_party_profanity ("these people can fuck off")
    - frustration_exclamations ("fuck this")
    - playful_expressions ("you son of a bitch!")
    - etc.
    
    Used to prevent must_escalate on comments that contain insult words
    but are clearly not personal attacks.
    """
    text_lower = text.lower()
    
    # Check all benign_skip categories
    benign_skip = PATTERNS.get("benign_skip", {})
    for category, phrases in benign_skip.items():
        if category.startswith("_"):
            continue
        if not isinstance(phrases, list):
            continue
        for phrase in phrases:
            if phrase.lower() in text_lower:
                return True
    
    return False

def is_benign_exclamation(text: str) -> bool:
    """
    Check if comment contains a benign phrase that indicates it's not toxic.
    
    Two-tier approach:
    1. For ANY length comment: Check if benign pattern matches AND not strongly directed
       - This catches "it's a fucking plane", "Fuckin Dementors", etc.
    2. For short comments (<=12 words): Also allow general benign phrases if no insults
    
    Key insight: If a specific benign pattern like "it's a fucking" matches,
    that's strong evidence the profanity is emphasis, not an attack.
    
    SAFETY: Even with benign patterns, we don't skip if there are:
    - Self-harm phrases (kill yourself, kys, etc.)
    - Threat phrases (I'll kill you, etc.)
    - Slurs (these bypass benign skip via must_escalate anyway)
    """
    # If strongly directed at someone, don't skip
    if is_strongly_directed(text):
        return False
    
    # SAFETY CHECK: Never skip if comment contains dangerous content
    # These should always go to ML/LLM review even if benign pattern present
    if contains_self_harm(text):
        return False
    if contains_threat(text):
        return False
    
    # Normalize text for matching
    text_lower = text.strip().lower()
    
    # Check regex patterns first (these are anchored, safe for any length)
    for pattern in BENIGN_PHRASES_RE:
        if pattern.match(text_lower):
            return True
    
    # NEW: Check specific benign patterns that indicate non-toxic intent
    # These are safe for any length because they're specific phrases
    # like "it's a fucking", "these people can", "fuckin dementors"
    if matches_any_benign_pattern(text):
        return True
    
    # For short comments without specific patterns, use more restrictive check
    word_count = len(text_lower.split())
    if word_count > 12:
        return False  # Too long for general benign phrase skip
    
    # Check for insults - if present, don't skip even with benign phrase
    if contains_direct_insult(text):
        return False
    
    # Now safe to do substring matching on short, non-insulting comments
    for phrase in BENIGN_PHRASES_SET:
        if phrase in text_lower:
            return True
    
    return False


# ============================================
# 6. EXTERNAL MODERATION API CLIENTS
# ============================================

class OpenAIModerationClient:
    """
    Client for OpenAI's free Moderation API.
    Detects: hate, harassment, self-harm, sexual, violence (with sub-categories).
    Includes rate limiting to avoid 429 errors.
    """
    
    # Categories and their thresholds for flagging
    DEFAULT_THRESHOLDS = {
        'hate': 0.5,
        'hate/threatening': 0.5,
        'harassment': 0.5,
        'harassment/threatening': 0.5,
        'self-harm': 0.3,  # Lower threshold for safety
        'self-harm/intent': 0.3,
        'self-harm/instructions': 0.3,
        'sexual': 0.8,  # Higher - less relevant for Reddit moderation
        'sexual/minors': 0.1,  # Very low - always flag
        'violence': 0.7,
        'violence/graphic': 0.7,
    }
    
    def __init__(self, api_key: str, threshold: float = 0.5, requests_per_minute: int = 30):
        self.api_key = api_key
        self.base_threshold = threshold
        self.available = bool(api_key)
        self.total_calls = 0
        self.flagged_count = 0
        self.errors = 0
        self.rate_limited_skips = 0
        self.client = None
        
        # Rate limiting
        self.requests_per_minute = requests_per_minute
        self.request_times = []
        
        if self.available:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=api_key)
                logging.info(f"OpenAI Moderation API enabled (threshold={threshold}, rate_limit={requests_per_minute}/min)")
            except ImportError:
                logging.warning("OpenAI library not installed. Run: pip install openai")
                self.available = False
            except Exception as e:
                logging.warning(f"Failed to initialize OpenAI client: {e}")
                self.available = False
        else:
            logging.info("OpenAI Moderation API not configured (no OPENAI_API_KEY)")
    
    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits. Returns True if OK to proceed."""
        now = time.time()
        # Remove requests older than 1 minute
        self.request_times = [t for t in self.request_times if now - t < 60]
        
        if len(self.request_times) >= self.requests_per_minute:
            return False
        
        self.request_times.append(now)
        return True
    
    def check_toxicity(self, text: str) -> Tuple[bool, float, Dict[str, float]]:
        """
        Check text for toxicity using OpenAI Moderation API.
        
        Returns:
            (is_flagged, max_score, all_scores)
            - is_flagged: True if any category exceeds its threshold
            - max_score: Highest score across all categories
            - all_scores: Dict of category -> score
        """
        if not self.available or not self.client:
            return False, 0.0, {}
        
        # Check rate limit before making request
        if not self._check_rate_limit():
            self.rate_limited_skips += 1
            if self.rate_limited_skips % 10 == 1:  # Log every 10th skip
                logging.debug(f"OpenAI Moderation rate limited, skipping (total skips: {self.rate_limited_skips})")
            return False, 0.0, {}
        
        try:
            self.total_calls += 1
            
            # Call OpenAI Moderation API
            response = self.client.moderations.create(
                model="omni-moderation-latest",
                input=text[:32000]  # API limit
            )
            
            result = response.results[0]
            
            # Extract scores
            scores = {}
            triggered_categories = []
            
            # Safety-critical categories that should NOT have thresholds raised
            SAFETY_CRITICAL = {'self-harm', 'self-harm/intent', 'self-harm/instructions', 'sexual/minors'}
            
            for category, score in result.category_scores.model_dump().items():
                scores[category] = score
                default_thresh = self.DEFAULT_THRESHOLDS.get(category, 0.5)
                
                # For safety-critical categories, always use the lower (more sensitive) threshold
                # For other categories, allow env var to raise threshold (less sensitive)
                if category in SAFETY_CRITICAL:
                    threshold = min(default_thresh, self.base_threshold)
                else:
                    threshold = max(default_thresh, self.base_threshold)
                
                if score >= threshold:
                    triggered_categories.append(f"{category}={score:.2f}")
            
            max_score = max(scores.values()) if scores else 0.0
            is_flagged = len(triggered_categories) > 0
            
            if is_flagged:
                self.flagged_count += 1
                logging.debug(f"OpenAI Moderation flagged ({', '.join(triggered_categories)}): {text[:50]}...")
            
            return is_flagged, max_score, scores
            
        except Exception as e:
            logging.warning(f"OpenAI Moderation API error: {e}")
            self.errors += 1
            return False, 0.0, {}


class PerspectiveAPIClient:
    """
    Client for Google's Perspective API.
    Detects: toxicity, severe_toxicity, identity_attack, insult, profanity, threat.
    Free to use with API key from Google Cloud.
    """
    
    # Attributes to request and their thresholds
    DEFAULT_THRESHOLDS = {
        'TOXICITY': 0.7,
        'SEVERE_TOXICITY': 0.5,
        'IDENTITY_ATTACK': 0.5,
        'INSULT': 0.7,
        'PROFANITY': 0.8,  # Higher - profanity alone isn't always bad
        'THREAT': 0.5,
    }
    
    DISCOVERY_URL = "https://commentanalyzer.googleapis.com/$discovery/rest?version=v1alpha1"
    
    def __init__(self, api_key: str, threshold: float = 0.7, requests_per_minute: int = 60):
        self.api_key = api_key
        self.base_threshold = threshold
        self.available = bool(api_key)
        self.total_calls = 0
        self.flagged_count = 0
        self.errors = 0
        self.rate_limited_skips = 0
        self.client = None
        
        # Rate limiting
        self.requests_per_minute = requests_per_minute
        self.request_times = []
        
        if self.available:
            try:
                from googleapiclient import discovery
                self.client = discovery.build(
                    "commentanalyzer",
                    "v1alpha1",
                    developerKey=api_key,
                    discoveryServiceUrl=self.DISCOVERY_URL,
                    static_discovery=False,
                )
                logging.info(f"Perspective API enabled (threshold={threshold}, rate_limit={requests_per_minute}/min)")
            except ImportError:
                logging.warning("Google API client not installed. Run: pip install google-api-python-client")
                self.available = False
            except Exception as e:
                logging.warning(f"Failed to initialize Perspective API client: {e}")
                self.available = False
        else:
            logging.info("Perspective API not configured (no PERSPECTIVE_API_KEY)")
    
    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits. Returns True if OK to proceed."""
        now = time.time()
        # Remove requests older than 1 minute
        self.request_times = [t for t in self.request_times if now - t < 60]
        
        if len(self.request_times) >= self.requests_per_minute:
            return False
        
        self.request_times.append(now)
        return True
    
    def check_toxicity(self, text: str) -> Tuple[bool, float, Dict[str, float]]:
        """
        Check text for toxicity using Perspective API.
        
        Returns:
            (is_flagged, max_score, all_scores)
            - is_flagged: True if any category exceeds its threshold
            - max_score: Highest score across all categories
            - all_scores: Dict of category -> score
        """
        if not self.available or not self.client:
            return False, 0.0, {}
        
        # Check rate limit before making request
        if not self._check_rate_limit():
            self.rate_limited_skips += 1
            if self.rate_limited_skips % 10 == 1:
                logging.debug(f"Perspective API rate limited, skipping (total skips: {self.rate_limited_skips})")
            return False, 0.0, {}
        
        try:
            self.total_calls += 1
            
            # Build request with all attributes
            analyze_request = {
                'comment': {'text': text[:20480]},  # API limit ~20KB
                'requestedAttributes': {attr: {} for attr in self.DEFAULT_THRESHOLDS.keys()},
                'doNotStore': True,  # Don't store for training
            }
            
            logging.info(f"HTTP Request: POST https://commentanalyzer.googleapis.com/v1alpha1/comments:analyze")
            response = self.client.comments().analyze(body=analyze_request).execute()
            
            # Extract scores
            scores = {}
            triggered_categories = []
            
            for attr, data in response.get('attributeScores', {}).items():
                score = data.get('summaryScore', {}).get('value', 0.0)
                scores[attr] = score
                # Use base_threshold from env if set higher than default (less sensitive)
                # or use default if it's already higher (e.g., PROFANITY=0.8)
                # This allows PERSPECTIVE_THRESHOLD to raise thresholds
                default_thresh = self.DEFAULT_THRESHOLDS.get(attr, 0.7)
                threshold = max(default_thresh, self.base_threshold)
                
                if score >= threshold:
                    triggered_categories.append(f"{attr}={score:.2f}")
            
            max_score = max(scores.values()) if scores else 0.0
            is_flagged = len(triggered_categories) > 0
            
            if is_flagged:
                self.flagged_count += 1
                logging.debug(f"Perspective API flagged ({', '.join(triggered_categories)}): {text[:50]}...")
            
            return is_flagged, max_score, scores
            
        except Exception as e:
            error_str = str(e)
            # Handle unsupported language gracefully (don't log as warning)
            if 'LANGUAGE_NOT_SUPPORTED' in error_str or 'does not support request languages' in error_str:
                logging.debug(f"Perspective API: language not supported, skipping")
                self.errors += 1
                return False, 0.0, {}
            
            logging.warning(f"Perspective API error: {e}")
            self.errors += 1
            return False, 0.0, {}


# ============================================
# 7. SMART PRE-FILTER CLASS
# ============================================

class PreFilterResult:
    """Result of pre-filtering"""
    MUST_ESCALATE = "MUST_ESCALATE"
    SEND_TO_LLM = "SEND_TO_LLM"
    SKIP = "SKIP"


class SmartPreFilter:
    """
    Multi-layered pre-filter that:
    1. Always escalates high-priority patterns (threats, slurs, accusations)
    2. Skips obviously benign enthusiasm
    3. Uses Detoxify with smart label-specific thresholds
    4. Optionally uses ModerateHatespeech API for additional/alternative scoring
    5. Considers directedness for borderline cases
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.model = None
        self.available = False
        self.openai_mod_client = None
        self.perspective_client = None
        
        # Initialize OpenAI Moderation client if enabled
        if config.openai_moderation_enabled and config.openai_moderation_key:
            self.openai_mod_client = OpenAIModerationClient(
                api_key=config.openai_moderation_key,
                threshold=config.openai_moderation_threshold,
                requests_per_minute=config.openai_moderation_rpm
            )
            logging.info(f"OpenAI Moderation mode: {config.openai_moderation_mode}")
        
        # Initialize Perspective API client if enabled
        if config.perspective_enabled and config.perspective_api_key:
            self.perspective_client = PerspectiveAPIClient(
                api_key=config.perspective_api_key,
                threshold=config.perspective_threshold,
                requests_per_minute=config.perspective_rpm
            )
            logging.info(f"Perspective API mode: {config.perspective_mode}")
        
        # Determine if we should skip Detoxify
        # Skip only if BOTH APIs are in "only" mode, or if one is "only" and the other is disabled
        self.skip_detoxify = False
        if config.openai_moderation_mode == "only" and config.openai_moderation_enabled:
            if not config.perspective_enabled or config.perspective_mode == "only":
                self.skip_detoxify = True
        if config.perspective_mode == "only" and config.perspective_enabled:
            if not config.openai_moderation_enabled or config.openai_moderation_mode == "only":
                self.skip_detoxify = True
        
        # Initialize Detoxify (unless we're skipping it)
        if not self.skip_detoxify:
            try:
                from detoxify import Detoxify
                logging.info(f"Loading Detoxify model '{config.detoxify_model}'...")
                self.model = Detoxify(config.detoxify_model)
                self.available = True
                logging.info(f"Detoxify model loaded successfully")
            except ImportError:
                logging.warning("Detoxify not installed. Using pattern matching only.")
            except Exception as e:
                logging.warning(f"Failed to load Detoxify: {e}. Using pattern matching only.")
        else:
            logging.info("Detoxify disabled (external API mode=only)")
        
        # Log pattern counts
        logging.info(f"SmartPreFilter patterns: {len(SLUR_WORDS)} slur words, {len(SLUR_PHRASES)} slur phrases, "
                     f"{len(CONTEXTUAL_WORDS)} contextual words, {len(CONTEXTUAL_PHRASES)} contextual phrases, "
                     f"{len(SELF_HARM_PHRASES)} self-harm, {len(THREAT_PHRASES)} threats, "
                     f"{len(INSULT_WORDS)} insult words, {len(INSULT_PHRASES)} insult phrases, "
                     f"{len(ACCUSATION_PHRASES)} accusations, {len(HARASSMENT_MOD_PHRASES)+len(HARASSMENT_CONDESCENSION_PHRASES)+len(HARASSMENT_EMOJI)} harassment, "
                     f"{len(DISMISSIVE_GATEKEEPING_PHRASES)} gatekeeping, {len(MUST_ESCALATE_RE)} regex patterns")
        
        # Log thresholds
        logging.info(f"Thresholds: threat={config.threshold_threat}, severe_toxicity={config.threshold_severe_toxicity}, "
                     f"identity_attack={config.threshold_identity_attack}, "
                     f"insult={config.threshold_insult_directed}/{config.threshold_insult_not_directed} (dir/not), "
                     f"toxicity={config.threshold_toxicity_directed}/{config.threshold_toxicity_not_directed} (dir/not), "
                     f"obscene={config.threshold_obscene}, borderline={config.threshold_borderline}")
        
        # Stats - load persisted values or start fresh
        persisted = load_pipeline_stats()
        self.total = persisted.get("total", 0)
        self.must_escalate = persisted.get("must_escalate", 0)
        self.ml_sent = persisted.get("ml_sent", 0)  # Comments sent due to ML layer (not double-counted)
        self.openai_mod_flagged = persisted.get("openai_mod_flagged", 0)  # Times OpenAI flagged (can overlap with others)
        self.perspective_flagged = persisted.get("perspective_flagged", 0)  # Times Perspective flagged (can overlap with others)
        self.detoxify_triggered = persisted.get("detoxify_triggered", 0)  # Times Detoxify triggered (can overlap with others)
        self.benign_skipped = persisted.get("benign_skipped", 0)
        self.pattern_skipped = persisted.get("pattern_skipped", 0)
        self._stats_save_counter = 0  # Save every N comments to reduce disk writes
        
        if persisted:
            logging.info(f"Loaded persisted pipeline stats: {self.total} total, {self.must_escalate + self.ml_sent} sent to LLM")
    
    def save_stats(self) -> None:
        """Persist current stats to disk"""
        save_pipeline_stats({
            "total": self.total,
            "must_escalate": self.must_escalate,
            "ml_sent": self.ml_sent,
            "openai_mod_flagged": self.openai_mod_flagged,
            "perspective_flagged": self.perspective_flagged,
            "detoxify_triggered": self.detoxify_triggered,
            "benign_skipped": self.benign_skipped,
            "pattern_skipped": self.pattern_skipped,
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        })
    
    def _maybe_save_stats(self) -> None:
        """Save stats periodically (every 10 comments) to reduce disk writes"""
        self._stats_save_counter += 1
        if self._stats_save_counter >= 10:
            self.save_stats()
            self._stats_save_counter = 0
    
    def _get_ml_scores(self, text: str, is_top_level: bool = False) -> Dict[str, float]:
        """
        Get ML scores from all available detectors (Detoxify, OpenAI, Perspective).
        Used to provide context to LLM even for pattern-matched comments.
        """
        scores = {}
        
        # Run Detoxify if available
        if self.available:
            try:
                results = self.model.predict(text)
                scores = {k: float(v) for k, v in results.items()}
            except Exception as e:
                logging.debug(f"Detoxify scoring failed in _get_ml_scores: {e}")
        
        # Run OpenAI Moderation if available (always run for context)
        if self.openai_mod_client and self.openai_mod_client.available:
            try:
                _, _, mod_scores = self.openai_mod_client.check_toxicity(text)
                for cat, score in mod_scores.items():
                    scores[f"openai_{cat}"] = score
            except Exception as e:
                logging.debug(f"OpenAI Moderation failed in _get_ml_scores: {e}")
        
        # Run Perspective if available (always run for context)
        if self.perspective_client and self.perspective_client.available:
            try:
                _, _, persp_scores = self.perspective_client.check_toxicity(text)
                for cat, score in persp_scores.items():
                    scores[f"perspective_{cat}"] = score
            except Exception as e:
                logging.debug(f"Perspective API failed in _get_ml_scores: {e}")
        
        return scores
    
    def should_analyze(self, text: str, is_top_level: bool = False) -> Tuple[bool, float, Dict[str, float]]:
        """
        Determine if comment should be sent to LLM.
        
        Args:
            text: The comment text to analyze
            is_top_level: Whether this is a top-level comment (not a reply)
        
        Returns:
            (should_send_to_llm, max_score, all_scores)
        """
        self.total += 1
        self._maybe_save_stats()  # Persist stats periodically
        text_preview = text[:80].replace('\n', ' ')
        
        # -----------------------------------------
        # Layer 1: Must-escalate patterns
        # -----------------------------------------
        
        must_escalate_reason = None
        
        # Check regex patterns
        for pattern in MUST_ESCALATE_RE:
            if pattern.search(text):
                must_escalate_reason = "must_escalate:regex_pattern"
                break
        
        # Check slurs (now handles both words and phrases)
        if not must_escalate_reason and contains_slur(text):
            must_escalate_reason = "must_escalate:slur"
        
        # Check self-harm
        if not must_escalate_reason and contains_self_harm(text):
            must_escalate_reason = "must_escalate:self-harm"
        
        # Check threats
        if not must_escalate_reason and contains_threat(text):
            must_escalate_reason = "must_escalate:threat"
        
        # Check veiled threats/omens (only if directed)
        if not must_escalate_reason and is_strongly_directed(text) and contains_veiled_threat(text):
            must_escalate_reason = "must_escalate:veiled_threat"
        
        # Check sexual violence
        if not must_escalate_reason and contains_sexual_violence(text):
            must_escalate_reason = "must_escalate:sexual_violence"
        
        # Check brigading/harassment calls
        if not must_escalate_reason and contains_brigading(text):
            must_escalate_reason = "must_escalate:brigading"
        
        # Check violence/illegal advocacy (e.g., "shoot it down", "shine a laser")
        if not must_escalate_reason and contains_violence_illegal(text):
            must_escalate_reason = "must_escalate:violence_illegal"
        
        # Check shill accusations (only if STRONGLY directed at someone)
        if not must_escalate_reason and is_strongly_directed(text) and contains_shill_accusation(text):
            must_escalate_reason = "must_escalate:shill_accusation"
        
        # Check vote manipulation accusations (only if directed)
        if not must_escalate_reason and is_strongly_directed(text) and contains_vote_manipulation(text):
            must_escalate_reason = "must_escalate:vote_manipulation"
        
        # Check homophobic pejorative usage (always escalate - slur-like)
        if not must_escalate_reason and contains_homophobic_pejorative(text):
            must_escalate_reason = "must_escalate:homophobic_pejorative"
        
        # Check dehumanizing insults (only if directed)
        if not must_escalate_reason and is_strongly_directed(text) and contains_dehumanizing(text):
            must_escalate_reason = "must_escalate:dehumanizing"
        
        # Check bad faith accusations (only if directed)
        if not must_escalate_reason and is_strongly_directed(text) and contains_accusation(text):
            must_escalate_reason = "must_escalate:accusation"
        
        # Check harassment patterns (mod accusations, condescension, emoji mockery)
        if not must_escalate_reason:
            has_harassment, harassment_type = contains_harassment(text)
            if has_harassment:
                if not matches_any_benign_pattern(text):
                    if harassment_type == "mod_accusation":
                        must_escalate_reason = "must_escalate:harassment_mod"
                    elif harassment_type == "condescension":
                        if is_strongly_directed(text) or not is_top_level:
                            context = "directed" if is_strongly_directed(text) else "reply"
                            must_escalate_reason = f"must_escalate:harassment_condescension+{context}"
                    elif harassment_type == "emoji":
                        if is_strongly_directed(text) or not is_top_level:
                            context = "directed" if is_strongly_directed(text) else "reply"
                            must_escalate_reason = f"must_escalate:harassment_emoji+{context}"
        
        # Check dismissive/hostile - now split into hard, soft, and gatekeeping
        # BUT skip if benign pattern matches (e.g., "this bullshit argument" is criticizing idea, not person)
        if not must_escalate_reason:
            has_dismissive, dismissive_type = contains_dismissive_hostile(text)
            if has_dismissive:
                # Check for benign patterns before escalating
                if not matches_any_benign_pattern(text):
                    if dismissive_type == "hard":
                        if is_strongly_directed(text) or not is_top_level:
                            context = "directed" if is_strongly_directed(text) else "reply"
                            must_escalate_reason = f"must_escalate:dismissive_hard+{context}"
                    elif dismissive_type == "gatekeeping":
                        # Gatekeeping is inherently directed - always escalate
                        must_escalate_reason = "must_escalate:dismissive_gatekeeping"
                    else:  # soft
                        if is_strongly_directed(text):
                            must_escalate_reason = "must_escalate:dismissive_soft+directed"
        
        # Check direct insults + strongly directed (or reply context)
        # BUT skip if the comment contains benign patterns that indicate it's not a personal attack
        if not must_escalate_reason and contains_direct_insult(text):
            # Check if this matches any benign_skip pattern (self-inclusive, profanity as emphasis, etc.)
            # This prevents escalating on "it's a fucking plane" or "we humans are stupid"
            if not matches_any_benign_pattern(text):
                if is_strongly_directed(text) or not is_top_level:
                    context = "directed" if is_strongly_directed(text) else "reply"
                    must_escalate_reason = f"must_escalate:insult+{context}"
        
        # If must_escalate triggered, still get ML scores for context, then return
        if must_escalate_reason:
            self.must_escalate += 1
            scores = self._get_ml_scores(text, is_top_level)
            scores["_trigger_reasons"] = must_escalate_reason
            logging.info(f"PREFILTER | MUST_ESCALATE ({must_escalate_reason}) | '{text_preview}...'")
            return True, 1.0, scores
        
        # -----------------------------------------
        # Layer 2: Check for benign phrase (but don't skip ML yet)
        # -----------------------------------------
        
        # Track if benign pattern matched - this will be used AFTER ML scoring
        # to help decide whether to send to LLM
        has_benign_pattern = is_benign_exclamation(text)
        
        # -----------------------------------------
        # Layer 3: ML Scoring (Detoxify + External APIs)
        # ALWAYS run ML even if benign pattern matched - we need to catch
        # cases like "it's a fucking plane, kill yourself"
        # -----------------------------------------
        
        scores = {}
        detoxify_triggered = False
        openai_mod_triggered = False
        perspective_triggered = False
        triggered_reasons = []
        
        # --- Run Detoxify (unless skipped) ---
        if not self.skip_detoxify and self.available:
            try:
                results = self.model.predict(text)
                scores = {k: float(v) for k, v in results.items()}
                
                # Use STRONG directedness for threshold lowering
                is_directed = is_strongly_directed(text)
                
                # For replies, weak directedness might matter more
                if not is_directed and not is_top_level and is_weakly_directed(text):
                    is_directed = True
                
                # Check contextual terms - escalate if directed OR identity_attack is elevated
                has_contextual = contains_contextual_term(text)
                identity_attack_score = scores.get('identity_attack', 0)
                
                if has_contextual and (is_directed or identity_attack_score > 0.25):
                    detoxify_triggered = True
                    reason = "directed" if is_directed else f"identity_attack={identity_attack_score:.2f}"
                    triggered_reasons.append(f"contextual+{reason}")
                
                # Thresholds per label from config
                thresholds = {
                    'threat': self.config.threshold_threat,
                    'severe_toxicity': self.config.threshold_severe_toxicity,
                    'identity_attack': self.config.threshold_identity_attack,
                    'insult': self.config.threshold_insult_directed if is_directed else self.config.threshold_insult_not_directed,
                    'toxicity': self.config.threshold_toxicity_directed if is_directed else self.config.threshold_toxicity_not_directed,
                    'obscene': self.config.threshold_obscene,
                }
                
                triggered_labels = []
                for label, score in scores.items():
                    threshold = thresholds.get(label, 0.7)
                    if score >= threshold:
                        # For categories with directed/not-directed thresholds, show which was used
                        if label in ('insult', 'toxicity'):
                            dir_label = "dir" if is_directed else "notdir"
                            triggered_labels.append(f"{label}({dir_label})={score:.2f}>{threshold:.2f}")
                        else:
                            triggered_labels.append(f"{label}={score:.2f}>{threshold:.2f}")
                
                if triggered_labels:
                    detoxify_triggered = True
                    triggered_reasons.append(f"detoxify:{','.join(triggered_labels)}")
                    
            except Exception as e:
                logging.warning(f"Detoxify scoring failed: {e}")
                scores = {}
        elif not self.skip_detoxify and not self.available:
            # No Detoxify available - check contextual terms as fallback
            if contains_contextual_term(text) and is_strongly_directed(text):
                detoxify_triggered = True
                triggered_reasons.append("contextual+directed(no-detoxify)")
        
        # --- Run External APIs based on their mode settings ---
        # Modes: "all" = every comment, "confirm" = only if Detoxify triggers, "only" = skip Detoxify
        
        # Helper to determine if we should call an API
        def should_call_api(mode: str) -> bool:
            if mode == "all":
                return True
            elif mode == "confirm":
                return detoxify_triggered or not self.available
            elif mode == "only":
                return True
            return False
        
        # --- Run OpenAI Moderation (if enabled) ---
        if self.openai_mod_client and self.openai_mod_client.available:
            if should_call_api(self.config.openai_moderation_mode):
                is_flagged, max_mod_score, mod_scores = self.openai_mod_client.check_toxicity(text)
                
                # Add OpenAI scores to our scores dict with prefix
                for cat, score in mod_scores.items():
                    scores[f"openai_{cat}"] = score
                
                if is_flagged:
                    openai_mod_triggered = True
                    self.openai_mod_flagged += 1
                    triggered_cats = [f"{k}={v:.2f}" for k, v in mod_scores.items() 
                                     if v >= self.openai_mod_client.DEFAULT_THRESHOLDS.get(k, 0.5)]
                    triggered_reasons.append(f"openai:{','.join(triggered_cats[:3])}")
        
        # --- Run Perspective API (if enabled) ---
        if self.perspective_client and self.perspective_client.available:
            if should_call_api(self.config.perspective_mode):
                is_flagged, max_persp_score, persp_scores = self.perspective_client.check_toxicity(text)
                
                # Add Perspective scores to our scores dict with prefix
                for cat, score in persp_scores.items():
                    scores[f"perspective_{cat}"] = score
                
                if is_flagged:
                    perspective_triggered = True
                    self.perspective_flagged += 1
                    triggered_cats = [f"{k}={v:.2f}" for k, v in persp_scores.items() 
                                     if v >= self.perspective_client.DEFAULT_THRESHOLDS.get(k, 0.7)]
                    triggered_reasons.append(f"perspective:{','.join(triggered_cats[:3])}")
        
        # --- Decision: Send to AI if any triggered ---
        # If detoxify_can_escalate is False, detoxify alone won't trigger send
        effective_detoxify_triggered = detoxify_triggered and self.config.detoxify_can_escalate
        
        # Count how many ML models triggered
        ml_triggers_count = sum([
            1 if effective_detoxify_triggered else 0,
            1 if openai_mod_triggered else 0,
            1 if perspective_triggered else 0
        ])
        
        # If ONLY Detoxify triggered (not OpenAI or Perspective), check external scores
        # Detoxify triggers on any profanity/edgy content, but isn't reliable for actual toxicity
        # Require external validation: OpenAI OR Perspective must also trigger OR have elevated scores
        if effective_detoxify_triggered and not openai_mod_triggered and not perspective_triggered:
            # Get OpenAI and Perspective max scores
            openai_max = max([v for k, v in scores.items() if k.startswith('openai_') and isinstance(v, float)], default=0.0)
            persp_max = max([v for k, v in scores.items() if k.startswith('perspective_') and isinstance(v, float)], default=0.0)
            external_max = max(openai_max, persp_max)
            
            # Skip if external APIs don't validate the concern (scores < 0.30)
            if external_max < 0.30:
                self.benign_skipped += 1
                scores_summary = self._format_scores_summary(scores)
                benign_note = " (has benign pattern)" if has_benign_pattern else ""
                logging.info(f"PREFILTER | SKIP (detox-only, external APIs low: OpenAI={openai_max:.2f}, Persp={persp_max:.2f}){benign_note} | {scores_summary} | '{text_preview}...'")
                return False, scores.get('toxicity', 0.0), scores
        
        # If ONLY OpenAI triggered (not Detoxify and not Perspective), check for benign patterns
        # OpenAI harassment scores often flag substantive criticism of ideas/public figures
        # If benign pattern matches AND not strongly directed, skip
        if openai_mod_triggered and not effective_detoxify_triggered and not perspective_triggered:
            if has_benign_pattern and not is_strongly_directed(text):
                # Check Perspective score - if it's also low, skip
                persp_max = max([v for k, v in scores.items() if k.startswith('perspective_') and isinstance(v, float)], default=0.0)
                if persp_max < 0.40:  # Perspective doesn't see it as toxic either
                    self.benign_skipped += 1
                    scores_summary = self._format_scores_summary(scores)
                    logging.info(f"PREFILTER | SKIP (openai-only, benign pattern + not directed, Persp={persp_max:.2f}) | {scores_summary} | '{text_preview}...'")
                    return False, scores.get('toxicity', 0.0), scores
        
        if effective_detoxify_triggered or openai_mod_triggered or perspective_triggered:
            # Count detoxify triggers (for stats, even if not used for escalation)
            if detoxify_triggered:
                self.detoxify_triggered += 1
            
            max_score = max([v for k, v in scores.items() if isinstance(v, (int, float))], default=0.5)
            
            # Build log message
            directed_str = "directed" if is_strongly_directed(text) else "not directed"
            top_level_str = "top-level" if is_top_level else "reply"
            triggers = " + ".join(triggered_reasons)
            
            # Note if detoxify was ignored
            if detoxify_triggered and not self.config.detoxify_can_escalate:
                triggers += " (detoxify ignored)"
            
            # Add trigger reasons to scores for Discord notification
            scores["_trigger_reasons"] = triggers
            
            # Track total ML-layer sends (not double-counted)
            self.ml_sent += 1
            
            # Build scores summary for logging
            scores_summary = self._format_scores_summary(scores)
            
            logging.info(f"PREFILTER | SEND ({triggers}) [{directed_str}, {top_level_str}] | {scores_summary} | '{text_preview}...'")
            return True, max_score, scores
        
        # --- None triggered: SKIP ---
        self.pattern_skipped += 1
        
        # Log if detoxify triggered but was ignored
        if detoxify_triggered and not self.config.detoxify_can_escalate:
            logging.debug(f"PREFILTER | SKIP (detoxify triggered but DETOXIFY_CAN_ESCALATE=false) | '{text_preview}...'")
        
        if scores:
            numeric_scores = {k: v for k, v in scores.items() if isinstance(v, (int, float))}
            if numeric_scores:
                max_score = max(numeric_scores.values())
                # Build scores summary for logging
                scores_summary = self._format_scores_summary(scores)
                logging.info(f"PREFILTER | SKIP | {scores_summary} | '{text_preview}...'")
                return False, max_score, scores
        
        logging.info(f"PREFILTER | SKIP (no triggers) | '{text_preview}...'")
        return False, 0.0, scores
    
    def _format_scores_summary(self, scores: Dict) -> str:
        """Format scores from all APIs into a readable summary string."""
        parts = []
        
        # Detoxify score (main toxicity)
        if 'toxicity' in scores:
            parts.append(f"Detox:{scores['toxicity']:.2f}")
        
        # OpenAI scores (show harassment which is most relevant)
        openai_scores = {k: v for k, v in scores.items() if k.startswith('openai_')}
        if openai_scores:
            # Get the max OpenAI category
            max_openai = max(openai_scores.values()) if openai_scores else 0
            harassment = scores.get('openai_harassment', 0)
            hate = scores.get('openai_hate', 0)
            parts.append(f"OpenAI:{max(harassment, hate, max_openai):.2f}")
        
        # Perspective scores (show TOXICITY which is most relevant)
        persp_scores = {k: v for k, v in scores.items() if k.startswith('perspective_')}
        if persp_scores:
            toxicity = scores.get('perspective_TOXICITY', 0)
            insult = scores.get('perspective_INSULT', 0)
            parts.append(f"Persp:{max(toxicity, insult):.2f}")
        
        return " | ".join(parts) if parts else "no scores"
    
    def get_stats(self) -> str:
        if self.total == 0:
            return "No comments processed yet"
        
        # Actual comments sent to LLM
        sent = self.must_escalate + self.ml_sent
        skipped = self.benign_skipped + self.pattern_skipped
        pct_skipped = (skipped / self.total) * 100 if self.total > 0 else 0
        
        # Build detailed breakdown of what triggered (can overlap)
        ml_details = []
        if self.detoxify_triggered > 0:
            ml_details.append(f"detoxify:{self.detoxify_triggered}")
        if self.openai_mod_flagged > 0:
            ml_details.append(f"openai:{self.openai_mod_flagged}")
        if self.perspective_flagged > 0:
            ml_details.append(f"perspective:{self.perspective_flagged}")
        
        ml_str = f", triggers: {'+'.join(ml_details)}" if ml_details else ""
        
        return (
            f"Total: {self.total} | "
            f"Sent to LLM: {sent} (must_escalate: {self.must_escalate}, ml: {self.ml_sent}{ml_str}) | "
            f"Skipped: {skipped} ({pct_skipped:.1f}%)"
        )


# Alias for backward compatibility
DetoxifyFilter = SmartPreFilter


# -------------------------------
# LLM Analysis
# -------------------------------

@dataclass
class AnalysisResult:
    """Result from LLM comment analysis"""
    verdict: Verdict
    reason: str
    confidence: str  # "high", "medium", "low"
    raw_response: str
    detoxify_score: float = 0.0  # Pre-filter score that triggered analysis


class LLMAnalyzer:
    """Uses Groq (free tier), x.ai Grok, or OpenAI GPT for toxicity analysis with context understanding"""
    
    # x.ai model prefixes - models starting with these use x.ai API
    XAI_MODEL_PREFIXES = ("grok-", "grok/")
    
    # OpenAI model prefixes - models starting with these use OpenAI API
    OPENAI_MODEL_PREFIXES = ("gpt-", "o1-", "o3-", "chatgpt-")
    
    def __init__(self, groq_api_key: str, model: str, guidelines: str, 
                 fallback_chain: List[str] = None, daily_limit: int = 240,
                 requests_per_minute: int = 2, xai_api_key: str = "",
                 xai_reasoning_effort: str = "low", groq_reasoning_effort: str = "medium",
                 openai_api_key: str = ""):
        # Groq client (always available)
        self.groq_client = Groq(api_key=groq_api_key)
        self.groq_reasoning_effort = groq_reasoning_effort
        
        # x.ai client (optional, for Grok models)
        self.xai_client = None
        self.xai_reasoning_effort = xai_reasoning_effort
        if xai_api_key:
            self.xai_client = OpenAI(api_key=xai_api_key, base_url="https://api.x.ai/v1")
            logging.info(f"x.ai Grok API configured (reasoning_effort={xai_reasoning_effort})")
        
        # OpenAI client (optional, for GPT models)
        self.openai_client = None
        if openai_api_key:
            self.openai_client = OpenAI(api_key=openai_api_key)
            logging.info("OpenAI GPT API configured for LLM analysis")
        
        # Generate a fixed conversation ID for x.ai cache persistence
        # This increases likelihood of cache hits across requests
        self.xai_conv_id = str(uuid.uuid4())
        logging.debug(f"x.ai conversation ID for caching: {self.xai_conv_id}")
        
        self.primary_model = model
        self.fallback_chain = fallback_chain or []
        self.daily_limit = daily_limit
        self.guidelines = guidelines
        self.requests_per_minute = requests_per_minute
        
        # Track daily usage
        self.daily_calls = 0
        self.last_reset_date = time.strftime("%Y-%m-%d")
        
        # Rate limiting - track last request time
        self.last_request_time = 0
        self.min_request_interval = 60.0 / requests_per_minute  # seconds between requests
        
        # Model cooldowns - track when each model can be used again
        # Key: model name, Value: timestamp when cooldown expires
        self.model_cooldowns: Dict[str, float] = {}
        
        # Total stats
        self.api_calls = 0
    
    def _is_xai_model(self, model: str) -> bool:
        """Check if a model should use x.ai API"""
        return model.lower().startswith(self.XAI_MODEL_PREFIXES)
    
    def _is_openai_model(self, model: str) -> bool:
        """Check if a model should use OpenAI API"""
        return model.lower().startswith(self.OPENAI_MODEL_PREFIXES)
    
    def _get_client_for_model(self, model: str):
        """Get the appropriate client for a model"""
        if self._is_xai_model(model):
            if not self.xai_client:
                raise ValueError(f"Model {model} requires XAI_API_KEY to be set")
            return self.xai_client
        if self._is_openai_model(model):
            if not self.openai_client:
                raise ValueError(f"Model {model} requires OPENAI_API_KEY to be set")
            return self.openai_client
        return self.groq_client
    
    def _wait_for_rate_limit(self) -> None:
        """Wait if needed to respect rate limit"""
        now = time.time()
        time_since_last = now - self.last_request_time
        
        if time_since_last < self.min_request_interval:
            wait_time = self.min_request_interval - time_since_last
            logging.debug(f"Rate limiting: waiting {wait_time:.1f}s before next Groq request")
            time.sleep(wait_time)
        
        self.last_request_time = time.time()
    
    def _get_current_model(self) -> str:
        """Get the model to use - always returns primary, fallback handled in analyze()"""
        today = time.strftime("%Y-%m-%d")
        
        # Reset counter if it's a new day
        if today != self.last_reset_date:
            logging.info(f"New day detected - resetting daily counter (was {self.daily_calls})")
            self.daily_calls = 0
            self.last_reset_date = today
            # Clear all cooldowns on new day
            self.model_cooldowns.clear()
        
        return self.primary_model
    
    def _parse_retry_time(self, time_str: str) -> float:
        """
        Parse retry wait time from Groq error messages or headers.
        Examples: "24h0m0s", "5m20.3712s", "try again in 30s", "2m5s", "220ms"
        Returns seconds as float, or None if not parseable.
        """
        if not time_str:
            return None
            
        import re
        
        time_str_lower = time_str.lower()
        
        # Try to find time pattern anywhere in string (handles "try again in X" format)
        # Pattern handles: 24h0m0s, 5m20s, 30s, 220ms
        match = re.search(r'(\d+h)?(\d+m(?!s))?(\d+(?:\.\d+)?s)?(\d+ms)?', time_str_lower)
        if not match:
            return None
        
        total_seconds = 0.0
        
        hours_part = match.group(1)
        minutes_part = match.group(2)
        seconds_part = match.group(3)
        ms_part = match.group(4)
        
        if hours_part:
            hours = float(hours_part.rstrip('h'))
            total_seconds += hours * 3600
        
        if minutes_part:
            minutes = float(minutes_part.rstrip('m'))
            total_seconds += minutes * 60
        
        if seconds_part:
            seconds = float(seconds_part.rstrip('s'))
            total_seconds += seconds
            
        if ms_part:
            ms = float(ms_part.rstrip('ms'))
            total_seconds += ms / 1000
        
        return total_seconds if total_seconds > 0 else None
    
    def _check_rate_limit_headers(self, model: str, headers) -> None:
        """
        Check rate limit headers from Groq response and preemptively set cooldowns.
        
        Headers available:
        - x-ratelimit-remaining-requests: RPD remaining
        - x-ratelimit-remaining-tokens: TPM remaining  
        - x-ratelimit-reset-requests: When RPD resets (could be rolling window)
        - x-ratelimit-reset-tokens: When TPM resets
        """
        try:
            if not headers:
                return
            
            remaining_requests = headers.get('x-ratelimit-remaining-requests')
            remaining_tokens = headers.get('x-ratelimit-remaining-tokens')
            reset_requests = headers.get('x-ratelimit-reset-requests')
            
            # Log remaining quota at debug level
            if remaining_requests:
                logging.debug(f"{model} - Remaining requests (RPD): {remaining_requests}, resets in: {reset_requests}")
            if remaining_tokens:
                logging.debug(f"{model} - Remaining tokens (TPM): {remaining_tokens}")
            
            # Preemptively set cooldown if running low on requests
            if remaining_requests:
                try:
                    remaining = int(remaining_requests)
                    if remaining <= 5:
                        # Running very low - set moderate cooldown (10 min)
                        # Don't use full reset time since limits may be rolling
                        cooldown_time = 600  # 10 minutes
                        self.model_cooldowns[model] = time.time() + cooldown_time
                        logging.warning(f"⚠️ {model} nearly exhausted ({remaining} requests left) - 10m cooldown, will retry")
                    elif remaining <= 20:
                        logging.info(f"📊 {model} has {remaining} requests remaining")
                except (ValueError, TypeError):
                    pass
                    
        except Exception as e:
            # Don't fail on header parsing errors
            logging.debug(f"Could not parse rate limit headers: {e}")
    
    def _build_ml_scores_context(self, scores: Dict[str, float]) -> str:
        """Build a context string describing ML detector scores for the LLM."""
        if not scores:
            return ""
        
        lines = ["[ML DETECTOR SCORES - Multiple models analyzed this comment:]"]
        
        # Detoxify scores (local ML model)
        detoxify_scores = {k: v for k, v in scores.items() 
                          if not k.startswith(('openai_', 'perspective_', '_')) and isinstance(v, (int, float))}
        if detoxify_scores:
            top_scores = sorted(detoxify_scores.items(), key=lambda x: x[1], reverse=True)[:4]
            score_strs = [f"{k}={v:.2f}" for k, v in top_scores if v > 0.1]
            if score_strs:
                lines.append(f"")
                lines.append(f"  DETOXIFY (local ML model - fast pre-filter, can have false positives on profanity):")
                lines.append(f"    Scores: {', '.join(score_strs)}")
                lines.append(f"    Thresholds: insult≥0.40 (directed)/0.65 (general), toxicity≥0.50/0.65, threat≥0.15")
        
        # OpenAI Moderation scores
        openai_scores = {k.replace('openai_', ''): v for k, v in scores.items() 
                        if k.startswith('openai_') and isinstance(v, (int, float))}
        if openai_scores:
            top_scores = sorted(openai_scores.items(), key=lambda x: x[1], reverse=True)[:4]
            score_strs = [f"{k}={v:.2f}" for k, v in top_scores if v > 0.1]
            if score_strs:
                lines.append(f"")
                lines.append(f"  OPENAI MODERATION (highly accurate for harassment, hate speech, self-harm, violence):")
                lines.append(f"    Scores: {', '.join(score_strs)}")
                lines.append(f"    Thresholds: harassment≥0.50, hate≥0.50, violence≥0.70, self-harm≥0.30")
        
        # Perspective API scores
        perspective_scores = {k.replace('perspective_', ''): v for k, v in scores.items() 
                             if k.startswith('perspective_') and isinstance(v, (int, float))}
        if perspective_scores:
            top_scores = sorted(perspective_scores.items(), key=lambda x: x[1], reverse=True)[:4]
            score_strs = [f"{k}={v:.2f}" for k, v in top_scores if v > 0.1]
            if score_strs:
                lines.append(f"")
                lines.append(f"  GOOGLE PERSPECTIVE (highly accurate for toxicity, trained on millions of comments):")
                lines.append(f"    Scores: {', '.join(score_strs)}")
                lines.append(f"    Thresholds: TOXICITY≥0.70, INSULT≥0.70, SEVERE_TOXICITY≥0.50, THREAT≥0.50")
        
        # Check trigger reasons if available
        trigger = scores.get('_trigger_reasons', '')
        if trigger and 'must_escalate' in trigger:
            lines.append(f"")
            lines.append(f"  ⚠️ PATTERN MATCH: {trigger}")
            lines.append(f"    (Direct pattern match on slurs, threats, or targeted insults)")
        
        if len(lines) > 1:
            lines.append(f"")
            lines.append(f"  GUIDANCE: OpenAI and Google Perspective are more accurate for true toxicity.")
            lines.append(f"  High scores from multiple models = stronger signal. Use your judgment on context.")
            return '\n'.join(lines)
        return ""
    
    def analyze(self, text: str, subreddit: str, context_info: Dict[str, str] = None, 
                detoxify_score: float = 0.0, is_top_level: bool = False, 
                scores: Dict[str, float] = None) -> AnalysisResult:
        """Send to Groq for nuanced analysis"""
        
        # Extract context info
        context_info = context_info or {}
        post_title = context_info.get("post_title", "")
        parent_context = context_info.get("parent_context", "")
        parent_author = context_info.get("parent_author", "")
        is_parent_op = context_info.get("is_parent_op", False)
        grandparent_context = context_info.get("grandparent_context", "")
        grandparent_author = context_info.get("grandparent_author", "")
        
        current_model = self._get_current_model()
        
        system_prompt = f"""{self.guidelines}"""

        # Add context about comment type for accurate reasoning
        if is_top_level:
            context_note = "[TOP-LEVEL COMMENT on a post - not replying to another user]"
        else:
            if parent_author:
                if is_parent_op:
                    context_note = f"[REPLY to u/{parent_author} who is the OP (original poster)]"
                else:
                    context_note = f"[REPLY to u/{parent_author}'s comment]"
            else:
                context_note = "[REPLY to another user's comment]"
        
        # Check if comment contains Reddit-style quotes
        has_quotes = '\n>' in text or text.startswith('>')
        if has_quotes:
            context_note += "\n[CONTAINS QUOTED TEXT - lines starting with '>' are quoting another user, not the commenter's own words]"
        
        # Build ML scores context for the LLM
        ml_context = self._build_ml_scores_context(scores)
        if ml_context:
            context_note += f"\n\n{ml_context}"
        
        # Build user prompt with full context
        user_prompt = f"{context_note}\n"
        
        if post_title:
            user_prompt += f"\nPost title: \"{post_title}\"\n"
        
        # Add grandparent context first (if available) for full conversation flow
        if grandparent_context:
            gp_author_str = f"u/{grandparent_author}" if grandparent_author else "another user"
            user_prompt += f"\nGrandparent comment (from {gp_author_str}):\n> {grandparent_context[:500]}\n"
        
        if parent_context:
            if is_top_level:
                user_prompt += f"\nPost body:\n> {parent_context[:1000]}\n"
            else:
                pa_author_str = f"u/{parent_author}" if parent_author else "another user"
                op_note = " [OP]" if is_parent_op else ""
                user_prompt += f"\nParent comment (from {pa_author_str}{op_note}):\n> {parent_context[:1000]}\n"
        
        user_prompt += f"\nAnalyze this comment:\n\n{text}"

        # Debug: log what we're sending
        logging.debug(f"GROQ SYSTEM PROMPT LENGTH: {len(system_prompt)} chars")
        logging.debug(f"GROQ USER PROMPT: {user_prompt[:500]}")
        logging.debug(f"GROQ MODEL: {current_model} (daily calls: {self.daily_calls}/{self.daily_limit})")

        try:
            # Wait if needed to respect our own rate limit
            self._wait_for_rate_limit()
            
            self.api_calls += 1
            self.daily_calls += 1
            
            # Start with configured model first, then fall through to fallback chain
            models_to_try = [current_model]
            for m in self.fallback_chain:
                if m not in models_to_try:
                    models_to_try.append(m)
            
            last_error = None
            response = None
            success = False
            fallback_delay = 30  # seconds to wait between fallback models
            
            for model_idx, model_to_use in enumerate(models_to_try):
                if success:
                    break
                
                # Check if model is on cooldown
                cooldown_until = self.model_cooldowns.get(model_to_use, 0)
                if time.time() < cooldown_until:
                    remaining = int(cooldown_until - time.time())
                    logging.info(f"Skipping {model_to_use} - on cooldown for {remaining}s more")
                    continue
                
                # Wait before trying fallback models (not for the first model)
                if model_idx > 0:
                    logging.info(f"Waiting {fallback_delay}s before trying fallback model...")
                    time.sleep(fallback_delay)
                    
                # Retry logic for each model
                max_retries = 2 if model_idx > 0 else 3  # Fewer retries for fallbacks
                retry_delay = 3  # seconds
                
                logging.info(f"Trying model {model_idx + 1}/{len(models_to_try)}: {model_to_use}")
                
                # Check if this is an x.ai model and we have the client
                is_xai = self._is_xai_model(model_to_use)
                is_openai = self._is_openai_model(model_to_use)
                
                if is_xai and not self.xai_client:
                    logging.warning(f"Skipping {model_to_use} - XAI_API_KEY not configured")
                    continue
                
                if is_openai and not self.openai_client:
                    logging.warning(f"Skipping {model_to_use} - OPENAI_API_KEY not configured for LLM")
                    continue
                
                for attempt in range(max_retries):
                    try:
                        if is_xai:
                            # x.ai API (OpenAI-compatible)
                            # Use conv_id header to improve prompt caching across requests
                            api_kwargs = {
                                "model": model_to_use,
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}
                                ],
                                "max_tokens": 200,
                                "temperature": 0.1,
                                "extra_headers": {"x-grok-conv-id": self.xai_conv_id}
                            }
                            
                            # Only grok-3-mini supports reasoning_effort parameter
                            # grok-4 is always a reasoning model (no parameter needed)
                            # grok-3 does not support reasoning_effort
                            if "grok-3-mini" in model_to_use.lower():
                                api_kwargs["extra_body"] = {"reasoning_effort": self.xai_reasoning_effort}
                            
                            response = self.xai_client.chat.completions.create(**api_kwargs)
                            raw_response = None  # No rate limit headers for x.ai
                        elif is_openai:
                            # OpenAI API (GPT models)
                            # Newer models (gpt-5, o1, o3) use max_completion_tokens instead of max_tokens
                            model_lower = model_to_use.lower()
                            use_new_param = any(x in model_lower for x in ['gpt-5', 'gpt-4.5', 'o1-', 'o3-'])
                            
                            api_kwargs = {
                                "model": model_to_use,
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}
                                ],
                                "temperature": 0.1,
                            }
                            
                            if use_new_param:
                                api_kwargs["max_completion_tokens"] = 200
                            else:
                                api_kwargs["max_tokens"] = 200
                            
                            response = self.openai_client.chat.completions.create(**api_kwargs)
                            raw_response = None  # Handle rate limits via exceptions
                        else:
                            # Groq API - use with_raw_response to get rate limit headers
                            groq_kwargs = {
                                "model": model_to_use,
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}
                                ],
                                "max_tokens": 200,
                                "temperature": 0.1,  # Low temp for consistent classification
                            }
                            
                            # Add reasoning parameters for models that support it
                            model_lower = model_to_use.lower()
                            if "qwen3" in model_lower or "qwen/qwen3" in model_lower:
                                # Qwen3 uses reasoning_format, always reasons by default
                                groq_kwargs["reasoning_format"] = "hidden"  # Don't show <think> tags
                            elif "gpt-oss" in model_lower or "openai/gpt-oss" in model_lower:
                                # GPT-OSS supports reasoning_effort (low/medium/high)
                                groq_kwargs["reasoning_effort"] = self.groq_reasoning_effort
                            elif "deepseek-r1" in model_lower:
                                # DeepSeek R1 always reasons, hide the output
                                groq_kwargs["reasoning_format"] = "hidden"
                            
                            raw_response = self.groq_client.chat.completions.with_raw_response.create(**groq_kwargs)
                            response = raw_response.parse()
                        
                        if model_to_use != models_to_try[0]:
                            logging.info(f"Successfully used fallback model: {model_to_use}")
                        # Clear any cooldown on success
                        if model_to_use in self.model_cooldowns:
                            del self.model_cooldowns[model_to_use]
                        
                        # Check rate limit headers from response (Groq only)
                        if raw_response and hasattr(raw_response, 'headers'):
                            self._check_rate_limit_headers(model_to_use, raw_response.headers)
                        
                        success = True
                        break  # Success - exit retry loop
                        
                    except Exception as e:
                        error_str = str(e)
                        last_error = e
                        if "429" in error_str or "rate_limit" in error_str.lower():
                            # Log full error for debugging
                            logging.debug(f"Full rate limit error: {error_str}")
                            
                            # Check if daily limit is fully exhausted (Used == Limit)
                            daily_exhausted = False
                            import re
                            rpd_match = re.search(r'Limit (\d+), Used (\d+)', error_str)
                            if rpd_match:
                                limit = int(rpd_match.group(1))
                                used = int(rpd_match.group(2))
                                if used >= limit:
                                    daily_exhausted = True
                                    logging.warning(f"⚠️ {model_to_use} daily limit EXHAUSTED ({used}/{limit} RPD)")
                            
                            # Try to get retry-after from exception response headers first
                            suggested_wait = None
                            if hasattr(e, 'response') and hasattr(e.response, 'headers'):
                                retry_after = e.response.headers.get('retry-after')
                                if retry_after:
                                    try:
                                        suggested_wait = float(retry_after)
                                        logging.debug(f"Got retry-after header: {suggested_wait}s")
                                    except (ValueError, TypeError):
                                        pass
                            
                            # Fall back to parsing error message
                            if not suggested_wait:
                                suggested_wait = self._parse_retry_time(error_str)
                                if suggested_wait:
                                    logging.debug(f"Parsed wait time from message: {suggested_wait:.0f}s")
                            
                            if not suggested_wait:
                                logging.debug(f"Could not parse wait time from error")
                            
                            # If daily limit exhausted, set 1 hour cooldown regardless of retry-after
                            if daily_exhausted:
                                cooldown_time = 3600  # 1 hour
                                self.model_cooldowns[model_to_use] = time.time() + cooldown_time
                                logging.warning(f"Rate limited on {model_to_use} - daily limit exhausted, 1h cooldown set, trying next model...")
                                break  # Exit retry loop, try next model
                            # If wait time is short (< 30s), wait and retry same model
                            elif suggested_wait and suggested_wait <= 30 and attempt < max_retries - 1:
                                logging.warning(f"Rate limited on {model_to_use}, waiting {suggested_wait:.0f}s (from API) before retry {attempt + 2}/{max_retries}")
                                time.sleep(suggested_wait)
                                continue
                            elif suggested_wait and suggested_wait > 30:
                                # Set cooldown - minimum 120s, plus 60s buffer on top of API time
                                # Cap at 1 hour - if longer, we'll just retry and get a fresh wait time
                                cooldown_time = min(max(suggested_wait + 60, 120), 3600)
                                self.model_cooldowns[model_to_use] = time.time() + cooldown_time
                                logging.warning(f"Rate limited on {model_to_use} for {suggested_wait:.0f}s - {cooldown_time:.0f}s cooldown set, trying next model...")
                                break  # Exit retry loop, try next model
                            elif attempt < max_retries - 1:
                                wait_time = retry_delay * (attempt + 1)
                                logging.warning(f"Rate limited on {model_to_use}, waiting {wait_time}s before retry {attempt + 2}/{max_retries}")
                                time.sleep(wait_time)
                                continue
                            else:
                                # Out of retries for this model, set longer cooldown (10 min)
                                # since we couldn't parse the wait time
                                self.model_cooldowns[model_to_use] = time.time() + 600
                                logging.warning(f"Rate limit exhausted for {model_to_use}, 10m cooldown set, trying next model...")
                                break  # Exit retry loop, try next model
                        else:
                            # Non-rate-limit error - log and try next model
                            logging.warning(f"Error on {model_to_use}: {e}, trying next model...")
                            break
            
            if not success:
                # All models exhausted
                raise last_error or Exception("All models rate limited")
            
            raw = response.choices[0].message.content.strip()
            
            # Log cache usage for x.ai requests
            if hasattr(response, 'usage') and response.usage:
                usage = response.usage
                prompt_tokens = getattr(usage, 'prompt_tokens', 0)
                completion_tokens = getattr(usage, 'completion_tokens', 0)
                
                # Check for cached tokens (x.ai specific)
                cached_tokens = 0
                if hasattr(usage, 'prompt_tokens_details') and usage.prompt_tokens_details:
                    cached_tokens = getattr(usage.prompt_tokens_details, 'cached_tokens', 0)
                
                if cached_tokens > 0:
                    cache_pct = 100 * cached_tokens / prompt_tokens if prompt_tokens > 0 else 0
                    logging.info(f"LLM USAGE: {prompt_tokens} prompt ({cached_tokens} cached = {cache_pct:.1f}%), {completion_tokens} completion")
                else:
                    logging.debug(f"LLM USAGE: {prompt_tokens} prompt (no cache), {completion_tokens} completion")
            
            # Debug: log raw response
            logging.debug(f"GROQ RAW RESPONSE: {raw}")
            
            # Parse the plain text response
            # Expected format:
            # VERDICT: REPORT | BENIGN
            # REASON: <short explanation>
            
            verdict = Verdict.BENIGN  # Default
            reason = ""
            
            # Normalize the response - handle variations in formatting
            raw_upper = raw.upper()
            
            # Look for verdict
            if 'VERDICT:' in raw_upper or 'VERDICT :' in raw_upper:
                for line in raw.split('\n'):
                    line_stripped = line.strip()
                    line_upper = line_stripped.upper()
                    if line_upper.startswith('VERDICT'):
                        # Extract the verdict value
                        parts = line_stripped.split(':', 1)
                        if len(parts) > 1:
                            verdict_str = parts[1].strip().upper()
                            if 'REPORT' in verdict_str:
                                verdict = Verdict.REPORT
                            else:
                                verdict = Verdict.BENIGN
                    elif line_upper.startswith('REASON'):
                        parts = line_stripped.split(':', 1)
                        if len(parts) > 1:
                            reason = parts[1].strip()
            else:
                # Fallback: look for REPORT or BENIGN anywhere in response
                if 'REPORT' in raw_upper and 'BENIGN' not in raw_upper:
                    verdict = Verdict.REPORT
                else:
                    verdict = Verdict.BENIGN
            
            # Safeguard: if reason is empty or invalid, use a default
            if not reason or reason.upper() in ['REPORT', 'BENIGN', 'N/A', 'NONE']:
                reason = "Flagged for moderator review" if verdict == Verdict.REPORT else "No issues detected"
            
            return AnalysisResult(
                verdict=verdict,
                reason=reason,
                confidence="high",  # Not used anymore but kept for compatibility
                raw_response=raw,
                detoxify_score=detoxify_score
            )
            
        except Exception as e:
            logging.error(f"LLM analysis failed after trying all models: {e}")
            
            # Default to BENIGN when LLM unavailable - don't auto-report without LLM verification
            # But log prominently so it can be manually reviewed
            if detoxify_score >= 0.7:
                logging.warning(f"⚠️ HIGH DETOXIFY SCORE ({detoxify_score:.2f}) but LLM unavailable - SKIPPING (not reporting)")
                logging.warning(f"⚠️ This comment may need manual review")
            
            return AnalysisResult(
                verdict=Verdict.BENIGN,
                reason=f"LLM unavailable - skipped (detox={detoxify_score:.2f})",
                confidence="low",
                raw_response="",
                detoxify_score=detoxify_score
            )
    
    def get_stats(self) -> str:
        cooldowns = [m for m, t in self.model_cooldowns.items() if time.time() < t]
        cooldown_str = f", {len(cooldowns)} models on cooldown" if cooldowns else ""
        return f"LLM API calls: {self.api_calls} (today: {self.daily_calls}, primary: {self.primary_model}{cooldown_str})"


# -------------------------------
# Discord helper (enhanced)
# -------------------------------

FALSE_POSITIVES_FILE = "false_positives.json"

def post_discord(webhook: str, content: str) -> None:
    """Post a simple text message to Discord"""
    if not webhook:
        return
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ToxicReportBot/2.0"
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
    except Exception as e:
        logging.warning(f"Discord post failed: {e}")


def post_discord_embed(webhook: str, title: str, description: str, 
                       color: int = 0xFF0000, fields: List[Dict] = None,
                       url: str = None, footer: str = None) -> bool:
    """Post a rich embed message to Discord. Returns True on success."""
    if not webhook:
        return False
    
    embed = {
        "title": title[:256],  # Discord limit
        "description": description[:4096],  # Discord limit
        "color": color,
    }
    
    if url:
        embed["url"] = url
    
    if footer:
        embed["footer"] = {"text": footer[:2048]}  # Discord limit
    
    if fields:
        # Ensure fields have required structure and respect limits
        valid_fields = []
        for f in fields[:25]:  # Discord limit
            if isinstance(f, dict) and "name" in f and "value" in f:
                valid_fields.append({
                    "name": str(f["name"])[:256],  # Discord limit
                    "value": str(f["value"])[:1024],  # Discord limit
                    "inline": bool(f.get("inline", False))
                })
        if valid_fields:
            embed["fields"] = valid_fields
    
    embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    payload = {"embeds": [embed]}
    data = json.dumps(payload).encode("utf-8")
    
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ToxicReportBot/2.0"
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        return True
    except urllib.error.HTTPError as e:
        # Read error response for debugging
        error_body = ""
        try:
            error_body = e.read().decode('utf-8')
        except:
            pass
        logging.warning(f"Discord embed post failed: {e} - {error_body}")
        return False
    except Exception as e:
        logging.warning(f"Discord embed post failed: {e}")
        return False


def notify_discord_report(webhook: str, comment_text: str, permalink: str, 
                          reason: str, detoxify_score: float) -> None:
    """Send a Discord notification when a comment is reported"""
    if not webhook:
        return
    
    # Truncate comment for Discord (keep under embed limit)
    truncated = comment_text[:1500] + "..." if len(comment_text) > 1500 else comment_text
    
    fields = [
        {"name": "Reason", "value": reason[:1024], "inline": True},
        {"name": "Detoxify Score", "value": f"{detoxify_score:.2f}", "inline": True},
    ]
    
    post_discord_embed(
        webhook=webhook,
        title="🚨 Comment Reported",
        description=f"```{truncated}```",
        color=0xFF4444,  # Red
        fields=fields,
        url=permalink
    )


def notify_discord_auto_remove(webhook: str, comment_text: str, permalink: str,
                                reason: str, scores: Dict[str, float], 
                                auto_remove_reason: str) -> None:
    """Send a Discord notification when a comment is auto-removed"""
    if not webhook:
        return
    
    # Truncate comment for Discord
    truncated = comment_text[:1500] + "..." if len(comment_text) > 1500 else comment_text
    
    # Build scores summary
    score_parts = []
    
    # Detoxify max score
    detox_scores = {k: v for k, v in scores.items() 
                  if not k.startswith(('openai_', 'perspective_', '_')) and isinstance(v, (int, float))}
    if detox_scores:
        max_detox = max(detox_scores.values())
        score_parts.append(f"Detoxify: {max_detox:.2f}")
    
    # OpenAI max score
    openai_scores = {k.replace('openai_', ''): v for k, v in scores.items() 
                    if k.startswith('openai_') and isinstance(v, (int, float))}
    if openai_scores:
        max_openai = max(openai_scores.values())
        top_cat = max(openai_scores, key=openai_scores.get)
        score_parts.append(f"OpenAI: {max_openai:.2f} ({top_cat})")
    
    # Perspective max score
    persp_scores = {k.replace('perspective_', ''): v for k, v in scores.items() 
                   if k.startswith('perspective_') and isinstance(v, (int, float))}
    if persp_scores:
        max_persp = max(persp_scores.values())
        top_cat = max(persp_scores, key=persp_scores.get)
        score_parts.append(f"Perspective: {max_persp:.2f} ({top_cat})")
    
    scores_display = " | ".join(score_parts) if score_parts else "N/A"
    
    fields = [
        {"name": "Reason", "value": reason[:1024], "inline": False},
        {"name": "ML Scores", "value": scores_display, "inline": False},
        {"name": "Auto-Remove Trigger", "value": f"`{auto_remove_reason}`", "inline": False},
    ]
    
    post_discord_embed(
        webhook=webhook,
        title="🚫 REMOVED - Please Review",
        description=f"```{truncated}```",
        color=0x9B59B6,  # Purple to distinguish from regular reports
        fields=fields,
        url=permalink
    )


def notify_discord_llm_analysis(webhook: str, comment_text: str, permalink: str,
                                 detoxify_score: float, subreddit: str,
                                 trigger_reasons: str = None) -> None:
    """Send a Discord notification when a comment is sent to LLM for analysis"""
    if not webhook:
        return
    
    # Truncate comment for Discord
    truncated = comment_text[:800] + "..." if len(comment_text) > 800 else comment_text
    
    fields = [
        {"name": "Subreddit", "value": f"r/{subreddit}", "inline": True},
        {"name": "Max Score", "value": f"{detoxify_score:.2f}", "inline": True},
    ]
    
    # Add trigger reasons if available
    if trigger_reasons:
        # Truncate if too long for Discord field
        reasons_display = trigger_reasons[:200] + "..." if len(trigger_reasons) > 200 else trigger_reasons
        fields.append({"name": "Triggered By", "value": f"`{reasons_display}`", "inline": False})
    
    post_discord_embed(
        webhook=webhook,
        title="🔍 Analyzing Comment",
        description=f"```{truncated}```",
        color=0x3498DB,  # Blue
        fields=fields,
        url=permalink
    )


def notify_discord_verdict(webhook: str, verdict: str, reason: str, 
                           permalink: str) -> None:
    """Send a Discord notification with the LLM verdict"""
    if not webhook:
        return
    
    if verdict == "REPORT":
        color = 0xFF4444  # Red
        emoji = "🚨"
    else:
        color = 0x44FF44  # Green
        emoji = "✅"
    
    post_discord_embed(
        webhook=webhook,
        title=f"{emoji} Verdict: {verdict}",
        description=f"**Reason:** {reason}",
        color=color,
        url=permalink
    )


def notify_discord_borderline_skip(webhook: str, comment_text: str, permalink: str,
                                    detoxify_score: float, subreddit: str) -> None:
    """Send a Discord notification for borderline skipped comments"""
    if not webhook:
        return
    
    # Truncate comment for Discord
    truncated = comment_text[:800] + "..." if len(comment_text) > 800 else comment_text
    
    fields = [
        {"name": "Subreddit", "value": f"r/{subreddit}", "inline": True},
        {"name": "Detoxify Score", "value": f"{detoxify_score:.2f}", "inline": True},
        {"name": "Status", "value": "Skipped (below threshold)", "inline": True},
    ]
    
    post_discord_embed(
        webhook=webhook,
        title="⚪ Borderline Skip",
        description=f"```{truncated}```",
        color=0x808080,  # Gray
        fields=fields,
        url=permalink
    )


def notify_discord_daily_stats(webhook: str, stats: Dict) -> None:
    """
    Send statistics to Discord with clear scope separation.
    
    Scopes:
    1. Bot Pipeline - What the bot scanned/processed (persisted across restarts)
    2. Resolution Outcomes - 24h, 7 days, and all-time stats
    """
    if not webhook:
        return
    
    # --- Scope 1: Bot Pipeline Stats ---
    total_processed = stats.get("total_processed", 0)
    sent_to_llm = stats.get("sent_to_llm", 0)
    benign_skipped = stats.get("benign", 0)
    
    # Calculate LLM percentage safely
    llm_pct = (sent_to_llm / total_processed * 100) if total_processed > 0 else 0
    
    # --- Scope 2: Resolution Outcomes ---
    # Daily stats (last 24 hours)
    daily_stats = stats.get("accuracy_daily", {})
    daily_escalated = daily_stats.get("total_tracked", 0)
    daily_removed = daily_stats.get("removed", 0)
    daily_approved = daily_stats.get("approved", 0)
    daily_pending = daily_stats.get("pending", 0)
    daily_resolved = daily_removed + daily_approved
    daily_confirm_rate = (daily_removed / daily_resolved * 100) if daily_resolved > 0 else 0
    
    # Weekly stats (last 7 days)
    weekly_stats = stats.get("accuracy_weekly", {})
    weekly_escalated = weekly_stats.get("total_tracked", 0)
    weekly_removed = weekly_stats.get("removed", 0)
    weekly_approved = weekly_stats.get("approved", 0)
    weekly_pending = weekly_stats.get("pending", 0)
    weekly_resolved = weekly_removed + weekly_approved
    weekly_confirm_rate = (weekly_removed / weekly_resolved * 100) if weekly_resolved > 0 else 0
    
    # All-time stats
    alltime_stats = stats.get("accuracy_alltime", {})
    alltime_total = alltime_stats.get("total_tracked", 0)
    alltime_removed = alltime_stats.get("removed", 0)
    alltime_approved = alltime_stats.get("approved", 0)
    alltime_pending = alltime_stats.get("pending", 0)
    alltime_resolved = alltime_removed + alltime_approved
    alltime_confirm_rate = (alltime_removed / alltime_resolved * 100) if alltime_resolved > 0 else 0
    
    # Get recent false positives for display
    recent_fps = stats.get("recent_false_positives", [])
    
    # Build description with clear scope separation
    description = f"""**🤖 Pipeline** (cumulative)
• Scanned: {total_processed:,}
• Benign-skipped: {benign_skipped:,}
• Sent to LLM: {sent_to_llm:,} ({llm_pct:.1f}%)

**📋 Last 24 Hours** (reported)
• Escalated: {daily_escalated:,}
• Resolved: {daily_resolved:,} → Removed: {daily_removed:,} | Approved: {daily_approved:,}
• Pending: {daily_pending:,}

**📅 Last 7 Days** (reported)
• Escalated: {weekly_escalated:,}
• Resolved: {weekly_resolved:,} → Removed: {weekly_removed:,} | Approved: {weekly_approved:,}
• Pending: {weekly_pending:,}

**📊 All-Time**
• Escalated: {alltime_total:,}
• Resolved: {alltime_resolved:,} → Removed: {alltime_removed:,} | Approved: {alltime_approved:,}
• Pending: {alltime_pending:,}
"""
    
    # Add recent false positives if any
    if recent_fps:
        description += "\n**⚠️ Recent Approved** (potential FPs):\n"
        for fp in recent_fps[:3]:  # Show up to 3
            text_preview = fp.get("text", "")[:50].replace("\n", " ")
            reason = fp.get("groq_reason", "")[:40]
            link = fp.get("permalink", "")
            if link:
                # Shorten reddit link
                short_link = link.replace("https://reddit.com", "")
                description += f'• "{text_preview}..." - {reason}\n  {short_link}\n'
    
    # Precision fields - show formula
    if daily_resolved > 0:
        daily_precision = f"{daily_confirm_rate:.1f}%\n({daily_removed}/{daily_resolved})"
    else:
        daily_precision = "N/A"
    
    if weekly_resolved > 0:
        weekly_precision = f"{weekly_confirm_rate:.1f}%\n({weekly_removed}/{weekly_resolved})"
    else:
        weekly_precision = "N/A"
    
    if alltime_resolved > 0:
        alltime_precision = f"{alltime_confirm_rate:.1f}%\n({alltime_removed}/{alltime_resolved})"
    else:
        alltime_precision = "N/A"
    
    fields = [
        {
            "name": "🎯 24h Confirm", 
            "value": daily_precision, 
            "inline": True
        },
        {
            "name": "📅 7d Confirm", 
            "value": weekly_precision, 
            "inline": True
        },
        {
            "name": "📈 All-Time Confirm", 
            "value": alltime_precision, 
            "inline": True
        },
    ]
    
    footer_note = "Confirm Rate = Removed / (Removed + Approved). Pending = still in modqueue."
    
    # Color based on weekly confirm rate (or all-time if no weekly data)
    rate_to_use = weekly_confirm_rate if weekly_resolved > 0 else alltime_confirm_rate
    resolved_to_use = weekly_resolved if weekly_resolved > 0 else alltime_resolved
    
    if resolved_to_use == 0:
        color = 0x808080  # Gray - no data yet
    elif rate_to_use >= 80:
        color = 0x44FF44  # Green - high precision
    elif rate_to_use >= 60:
        color = 0xFFAA00  # Orange - moderate
    else:
        color = 0xFF4444  # Red - low precision
    
    post_discord_embed(
        webhook=webhook,
        title="📈 Moderation Stats",
        description=description,
        color=color,
        fields=fields,
        footer=footer_note
    )


# -------------------------------
# Discord Bot (Editable Review Messages)
# -------------------------------

def load_pending_reviews() -> List[Dict]:
    """Load pending review notifications from JSON file"""
    try:
        with open(PENDING_REVIEWS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {PENDING_REVIEWS_FILE}, starting fresh")
        return []


def save_pending_reviews(reviews: List[Dict]) -> None:
    """Save pending review notifications to JSON file"""
    with open(PENDING_REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, indent=2)


def add_pending_review(comment_id: str, discord_message_id: str, permalink: str, 
                       comment_text: str, reason: str, scores: Dict[str, float],
                       auto_remove_reason: str) -> None:
    """Add a new pending review to track"""
    reviews = load_pending_reviews()
    reviews.append({
        "comment_id": comment_id,
        "discord_message_id": discord_message_id,
        "permalink": permalink,
        "comment_text": comment_text[:500],  # Truncate for storage
        "reason": reason,
        "auto_remove_reason": auto_remove_reason,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scores": {k: v for k, v in scores.items() if isinstance(v, (int, float))}
    })
    save_pending_reviews(reviews)


def remove_pending_review(comment_id: str) -> None:
    """Remove a pending review after it's been resolved"""
    reviews = load_pending_reviews()
    reviews = [r for r in reviews if r.get("comment_id") != comment_id]
    save_pending_reviews(reviews)


def discord_bot_post_review(cfg: Config, comment_text: str, permalink: str,
                            reason: str, scores: Dict[str, float],
                            auto_remove_reason: str, author: str = "unknown") -> Optional[str]:
    """
    Post a review notification using Discord Bot API (not webhook).
    Returns the message ID so we can edit it later.
    """
    if not cfg.discord_bot_token or not cfg.discord_review_channel_id:
        return None
    
    # Truncate comment for Discord
    truncated = comment_text[:1500] + "..." if len(comment_text) > 1500 else comment_text
    
    # Build scores summary
    score_parts = []
    
    # Detoxify max score
    detox_scores = {k: v for k, v in scores.items() 
                  if not k.startswith(('openai_', 'perspective_', '_')) and isinstance(v, (int, float))}
    if detox_scores:
        max_detox = max(detox_scores.values())
        score_parts.append(f"Detoxify: {max_detox:.2f}")
    
    # OpenAI max score
    openai_scores = {k.replace('openai_', ''): v for k, v in scores.items() 
                    if k.startswith('openai_') and isinstance(v, (int, float))}
    if openai_scores:
        max_openai = max(openai_scores.values())
        top_cat = max(openai_scores, key=openai_scores.get)
        score_parts.append(f"OpenAI: {max_openai:.2f} ({top_cat})")
    
    # Perspective max score
    persp_scores = {k.replace('perspective_', ''): v for k, v in scores.items() 
                   if k.startswith('perspective_') and isinstance(v, (int, float))}
    if persp_scores:
        max_persp = max(persp_scores.values())
        top_cat = max(persp_scores, key=persp_scores.get)
        score_parts.append(f"Perspective: {max_persp:.2f} ({top_cat})")
    
    scores_display = " | ".join(score_parts) if score_parts else "N/A"
    
    # Build embed
    embed = {
        "title": "🔴 REMOVED - Needs Review",
        "description": f"```{truncated}```",
        "color": 0x9B59B6,  # Purple
        "fields": [
            {"name": "👤 Author", "value": f"u/{author}", "inline": True},
            {"name": "🔗 Link", "value": f"[View Comment]({permalink})", "inline": True},
            {"name": "📊 Reason", "value": reason[:1024], "inline": False},
            {"name": "🤖 ML Scores", "value": scores_display, "inline": False},
            {"name": "⚡ Trigger", "value": f"`{auto_remove_reason}`", "inline": False},
        ],
        "footer": {"text": "Bot will update this message when reviewed"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    
    url = f"https://discord.com/api/v10/channels/{cfg.discord_review_channel_id}/messages"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {cfg.discord_bot_token}",
            "User-Agent": "ToxicReportBot/1.0"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
            message_id = response_data.get("id")
            logging.info(f"Discord review notification posted (message_id: {message_id})")
            return message_id
    except Exception as e:
        logging.error(f"Failed to post Discord review notification: {e}")
        return None


def discord_bot_update_review(cfg: Config, message_id: str, status: str, 
                              mod_name: str, original_text: str, 
                              original_reason: str, permalink: str) -> bool:
    """
    Update an existing review notification with the resolution status.
    """
    if not cfg.discord_bot_token or not cfg.discord_review_channel_id or not message_id:
        return False
    
    # Truncate comment for Discord
    truncated = original_text[:1500] + "..." if len(original_text) > 1500 else original_text
    
    if status == "approved":
        title = "✅ APPROVED"
        color = 0x44FF44  # Green
        status_text = f"Approved by u/{mod_name}"
    elif status == "removed":
        title = "🗑️ CONFIRMED REMOVED"
        color = 0xFF4444  # Red
        status_text = f"Confirmed by u/{mod_name}"
    else:
        title = "❓ RESOLVED"
        color = 0x808080  # Gray
        status_text = f"Resolved by u/{mod_name}"
    
    embed = {
        "title": title,
        "description": f"~~```{truncated}```~~",  # Strikethrough
        "color": color,
        "fields": [
            {"name": "📋 Status", "value": status_text, "inline": True},
            {"name": "🔗 Link", "value": f"[View Comment]({permalink})", "inline": True},
            {"name": "📊 Original Reason", "value": original_reason[:500], "inline": False},
        ],
        "footer": {"text": f"Reviewed at {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    
    url = f"https://discord.com/api/v10/channels/{cfg.discord_review_channel_id}/messages/{message_id}"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {cfg.discord_bot_token}",
            "User-Agent": "ToxicReportBot/1.0"
        },
        method="PATCH"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logging.info(f"Discord review notification updated (message_id: {message_id}, status: {status})")
            return True
    except Exception as e:
        logging.error(f"Failed to update Discord review notification: {e}")
        return False


def check_pending_reviews(reddit: praw.Reddit, cfg: Config) -> None:
    """
    Check all pending reviews to see if they've been actioned by a mod.
    Updates Discord messages accordingly.
    """
    reviews = load_pending_reviews()
    if not reviews:
        return
    
    logging.info(f"Checking {len(reviews)} pending review(s)...")
    
    resolved = []
    
    for review in reviews:
        comment_id = review.get("comment_id", "")
        discord_message_id = review.get("discord_message_id", "")
        permalink = review.get("permalink", "")
        comment_text = review.get("comment_text", "")
        reason = review.get("reason", "")
        
        if not comment_id or not discord_message_id:
            resolved.append(comment_id)
            continue
        
        try:
            comment = reddit.comment(id=comment_id)
            comment._fetch()  # Force fetch to get current state
            
            # Check if comment was approved (removed=False after bot removed it)
            # or confirmed removed by mod
            mod_action = None
            mod_name = "a moderator"
            
            # Check modlog for this comment
            try:
                subreddit_name = permalink.split("/r/")[1].split("/")[0] if "/r/" in permalink else None
                if subreddit_name:
                    subreddit = reddit.subreddit(subreddit_name)
                    # Look for recent mod actions on this comment
                    for log_entry in subreddit.mod.log(limit=100):
                        if hasattr(log_entry, 'target_fullname') and log_entry.target_fullname == f"t1_{comment_id}":
                            if log_entry.action == "approvecomment":
                                mod_action = "approved"
                                mod_name = log_entry.mod.name if hasattr(log_entry, 'mod') else "a moderator"
                                break
                            elif log_entry.action == "removecomment":
                                # Check if this is a different mod confirming the removal
                                mod_action = "removed"
                                mod_name = log_entry.mod.name if hasattr(log_entry, 'mod') else "a moderator"
                                break
            except Exception as e:
                logging.debug(f"Could not check modlog for {comment_id}: {e}")
            
            # Fallback: check comment state directly
            if not mod_action:
                # If comment is no longer removed, it was approved
                if hasattr(comment, 'removed') and not comment.removed and hasattr(comment, 'banned_by') and comment.banned_by is None:
                    mod_action = "approved"
                # If comment body is [removed], it's still removed
                elif comment.body == "[removed]":
                    # Still removed, check if it's old enough to consider "confirmed"
                    created_at = review.get("created_at", "")
                    if created_at:
                        try:
                            created_time = time.mktime(time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ"))
                            # If it's been more than 24 hours and still removed, consider it confirmed
                            if time.time() - created_time > 86400:
                                mod_action = "removed"
                                mod_name = "timeout (24h)"
                        except:
                            pass
            
            if mod_action:
                # Update Discord message
                success = discord_bot_update_review(
                    cfg=cfg,
                    message_id=discord_message_id,
                    status=mod_action,
                    mod_name=mod_name,
                    original_text=comment_text,
                    original_reason=reason,
                    permalink=permalink
                )
                if success:
                    resolved.append(comment_id)
                    logging.info(f"Review resolved: {comment_id} -> {mod_action} by {mod_name}")
                    
        except prawcore.exceptions.NotFound:
            # Comment was deleted entirely
            discord_bot_update_review(
                cfg=cfg,
                message_id=discord_message_id,
                status="removed",
                mod_name="deletion",
                original_text=comment_text,
                original_reason=reason,
                permalink=permalink
            )
            resolved.append(comment_id)
            logging.info(f"Review resolved: {comment_id} -> deleted")
        except Exception as e:
            logging.debug(f"Error checking review {comment_id}: {e}")
    
    # Remove resolved reviews
    if resolved:
        reviews = [r for r in reviews if r.get("comment_id") not in resolved]
        save_pending_reviews(reviews)
        logging.info(f"Removed {len(resolved)} resolved review(s) from tracking")


def load_false_positives() -> List[Dict]:
    """Load false positives from JSON file"""
    try:
        with open(FALSE_POSITIVES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {FALSE_POSITIVES_FILE}, starting fresh")
        return []


def get_recent_false_positives(hours: int = 24, limit: int = 5) -> List[Dict]:
    """Get recent false positives for display in Discord stats"""
    entries = load_false_positives()
    if not entries:
        return []
    
    now = time.time()
    cutoff = now - (hours * 3600)
    
    recent = []
    for entry in entries:
        discovered_at = entry.get("discovered_at", "")
        if discovered_at:
            try:
                discovered_time = time.mktime(time.strptime(discovered_at, "%Y-%m-%dT%H:%M:%SZ"))
                if discovered_time >= cutoff:
                    recent.append(entry)
            except ValueError:
                pass
    
    # Sort by discovered_at descending (most recent first)
    recent.sort(key=lambda x: x.get("discovered_at", ""), reverse=True)
    return recent[:limit]


def save_false_positives(entries: List[Dict]) -> None:
    """Save false positives to JSON file"""
    with open(FALSE_POSITIVES_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def track_false_positive(comment_id: str, permalink: str, text: str,
                         groq_reason: str, detoxify_score: float,
                         reported_at: str, is_top_level: bool = False,
                         detoxify_scores: Dict[str, float] = None,
                         openai_scores: Dict[str, float] = None,
                         perspective_scores: Dict[str, float] = None,
                         context_info: Dict[str, str] = None) -> None:
    """Track a false positive (reported comment that was approved)"""
    entries = load_false_positives()
    
    # Don't add duplicates
    if any(e.get("comment_id") == comment_id for e in entries):
        return
    
    # Extract context info
    context_info = context_info or {}
    
    entries.append({
        "comment_id": comment_id,
        "permalink": permalink,
        "text": text[:1000],
        "groq_reason": groq_reason,
        "detoxify_score": detoxify_score,
        "detoxify_scores": detoxify_scores or {},
        "openai_scores": openai_scores or {},
        "perspective_scores": perspective_scores or {},
        "is_top_level": is_top_level,
        "post_title": context_info.get("post_title", ""),
        "parent_context": context_info.get("parent_context", "")[:500],
        "parent_author": context_info.get("parent_author", ""),
        "is_parent_op": context_info.get("is_parent_op", False),
        "grandparent_context": context_info.get("grandparent_context", "")[:300],
        "grandparent_author": context_info.get("grandparent_author", ""),
        "reported_at": reported_at,
        "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    
    save_false_positives(entries)
    logging.info(f"Tracked false positive: {comment_id}")


def check_and_track_false_positives(reddit: praw.Reddit, webhook: str = None) -> Dict[str, int]:
    """
    Check reported comments and track false positives.
    Returns stats and optionally notifies Discord.
    """
    comments = load_tracked_comments()
    now = time.time()
    stats = {"checked": 0, "removed": 0, "approved": 0, "still_pending": 0, "errors": 0}
    new_false_positives = []
    
    for entry in comments:
        if entry.get("outcome") != "pending":
            continue
        
        # Check if comment is old enough (24 hours)
        reported_at = entry.get("reported_at", "")
        if reported_at:
            try:
                reported_time = time.mktime(time.strptime(reported_at, "%Y-%m-%dT%H:%M:%SZ"))
                age_hours = (now - reported_time) / 3600
                if age_hours < 24:
                    stats["still_pending"] += 1
                    continue
            except ValueError:
                pass
        
        comment_id = entry.get("comment_id", "")
        if not comment_id:
            continue
            
        try:
            clean_id = comment_id.replace("t1_", "")
            comment = reddit.comment(clean_id)
            _ = comment.body  # Force fetch
            
            if comment.body == "[removed]" or getattr(comment, 'removed', False):
                entry["outcome"] = "removed"
                stats["removed"] += 1
            elif getattr(comment, 'removed_by_category', None):
                entry["outcome"] = "removed"
                stats["removed"] += 1
            else:
                # Comment still exists = approved/not actioned = false positive
                entry["outcome"] = "approved"
                stats["approved"] += 1
                
                # Track as false positive
                track_false_positive(
                    comment_id=comment_id,
                    permalink=entry.get("permalink", ""),
                    text=entry.get("text", ""),
                    groq_reason=entry.get("groq_reason", ""),
                    detoxify_score=entry.get("detoxify_score", 0),
                    reported_at=reported_at,
                    is_top_level=entry.get("is_top_level", False),
                    detoxify_scores=entry.get("detoxify_scores", {}),
                    openai_scores=entry.get("openai_scores", {}),
                    perspective_scores=entry.get("perspective_scores", {}),
                    context_info={
                        "post_title": entry.get("post_title", ""),
                        "parent_context": entry.get("parent_context", ""),
                        "parent_author": entry.get("parent_author", ""),
                        "is_parent_op": entry.get("is_parent_op", False),
                        "grandparent_context": entry.get("grandparent_context", ""),
                        "grandparent_author": entry.get("grandparent_author", ""),
                    }
                )
                new_false_positives.append(entry)
            
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["checked"] += 1
            
        except prawcore.exceptions.NotFound:
            entry["outcome"] = "removed"
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["removed"] += 1
            stats["checked"] += 1
        except Exception as e:
            logging.warning(f"Error checking comment {comment_id}: {e}")
            stats["errors"] += 1
    
    save_tracked_comments(comments)
    
    # Notify Discord about new false positives
    if webhook and new_false_positives:
        for fp in new_false_positives[:5]:  # Limit to 5 notifications
            post_discord_embed(
                webhook=webhook,
                title="⚠️ False Positive Detected",
                description=f"```{fp.get('text', '')[:500]}```",
                color=0xFFAA00,  # Orange
                fields=[
                    {"name": "Reason", "value": fp.get("groq_reason", "Unknown"), "inline": True},
                    {"name": "Detoxify", "value": f"{fp.get('detoxify_score', 0):.2f}", "inline": True},
                ],
                url=fp.get("permalink", "")
            )
    
    return stats


# -------------------------------
# Helper functions
# -------------------------------

def get_parent_context(thing) -> Dict[str, Any]:
    """
    Get parent context and post title for better analysis.
    Returns dict with: parent_context, post_title, parent_author, is_parent_op, grandparent_context
    """
    result = {
        "parent_context": "",
        "post_title": "",
        "parent_author": "",
        "is_parent_op": False,
        "grandparent_context": "",
        "grandparent_author": "",
    }
    
    try:
        op_author = ""
        
        # Get the submission (post) this comment is on
        if hasattr(thing, 'submission'):
            submission = thing.submission
            result["post_title"] = getattr(submission, 'title', '') or ''
            # Get OP's username for comparison
            if hasattr(submission, 'author') and submission.author:
                op_author = submission.author.name
        
        # Get immediate parent context
        if hasattr(thing, 'parent'):
            parent = thing.parent()
            
            if hasattr(parent, 'body'):
                # Parent is a comment
                result["parent_context"] = parent.body[:1000]
                
                # Get parent author
                if hasattr(parent, 'author') and parent.author:
                    result["parent_author"] = parent.author.name
                    result["is_parent_op"] = (parent.author.name == op_author)
                
                # Get grandparent context (one level up)
                if hasattr(parent, 'parent'):
                    try:
                        grandparent = parent.parent()
                        if hasattr(grandparent, 'body'):
                            result["grandparent_context"] = grandparent.body[:500]
                            if hasattr(grandparent, 'author') and grandparent.author:
                                result["grandparent_author"] = grandparent.author.name
                        elif hasattr(grandparent, 'selftext'):
                            # Grandparent is the submission
                            selftext = getattr(grandparent, 'selftext', '') or ""
                            if selftext:
                                result["grandparent_context"] = f"[POST] {selftext[:500]}"
                    except Exception:
                        pass
                        
            elif hasattr(parent, 'title'):
                # Parent is the submission itself (top-level comment)
                selftext = getattr(parent, 'selftext', '') or ""
                if selftext:
                    result["parent_context"] = selftext[:1000]
                if hasattr(parent, 'author') and parent.author:
                    result["parent_author"] = parent.author.name
                    result["is_parent_op"] = True  # Parent is OP's post
                    
    except Exception:
        pass
    
    return result


def get_text_from_thing(thing) -> Optional[str]:
    """Extract text content from a comment or submission"""
    # Comment
    if hasattr(thing, 'body'):
        body = thing.body
        if body and body != '[deleted]' and body != '[removed]':
            return body
    # Submission
    elif hasattr(thing, 'title'):
        title = getattr(thing, 'title', '') or ''
        selftext = getattr(thing, 'selftext', '') or ''
        text = f"{title.strip()}  {selftext.strip()}".strip()
        if text:
            return text
    return None


# -------------------------------
# Report
# -------------------------------

def check_auto_remove_consensus(scores: Dict[str, float], cfg: Config, is_pattern_match: bool = False) -> Tuple[bool, str]:
    """
    Check if comment meets consensus requirements for auto-removal.
    
    Returns:
        (should_auto_remove, reason_string)
    """
    if not cfg.auto_remove_enabled:
        return False, ""
    
    # Check pattern match override
    if is_pattern_match:
        if cfg.auto_remove_on_pattern_match:
            return True, "pattern_match"
        else:
            return False, ""
    
    # Check consensus from required models
    required_models = cfg.auto_remove_require_models
    
    # If no models required, auto-remove on LLM decision alone
    if not required_models or len(required_models) == 0:
        return True, "llm_decision"
    
    models_passed = []
    models_failed = []
    
    for model in required_models:
        model = model.lower()
        
        if model == "detoxify":
            # Get max Detoxify score (non-prefixed scores)
            detox_scores = {k: v for k, v in scores.items() 
                          if not k.startswith(('openai_', 'perspective_', '_')) and isinstance(v, (int, float))}
            max_detox = max(detox_scores.values()) if detox_scores else 0.0
            if max_detox >= cfg.auto_remove_detoxify_min:
                models_passed.append(f"detoxify={max_detox:.2f}")
            else:
                models_failed.append(f"detoxify={max_detox:.2f}<{cfg.auto_remove_detoxify_min}")
                
        elif model == "openai":
            # Get max OpenAI score
            openai_scores = {k: v for k, v in scores.items() 
                           if k.startswith('openai_') and isinstance(v, (int, float))}
            max_openai = max(openai_scores.values()) if openai_scores else 0.0
            if max_openai >= cfg.auto_remove_openai_min:
                models_passed.append(f"openai={max_openai:.2f}")
            else:
                models_failed.append(f"openai={max_openai:.2f}<{cfg.auto_remove_openai_min}")
                
        elif model == "perspective":
            # Get max Perspective score
            persp_scores = {k: v for k, v in scores.items() 
                          if k.startswith('perspective_') and isinstance(v, (int, float))}
            max_persp = max(persp_scores.values()) if persp_scores else 0.0
            if max_persp >= cfg.auto_remove_perspective_min:
                models_passed.append(f"perspective={max_persp:.2f}")
            else:
                models_failed.append(f"perspective={max_persp:.2f}<{cfg.auto_remove_perspective_min}")
    
    # Check if enough models passed
    if len(models_passed) >= cfg.auto_remove_min_consensus:
        return True, f"consensus:{'+'.join(models_passed)}"
    else:
        return False, f"no_consensus:{'+'.join(models_failed)}"


def file_report(thing, reason: str, cfg: Config) -> None:
    try:
        if cfg.report_as == "moderator":
            if cfg.report_rule_bucket:
                thing.mod.report(reason=reason, rule_name=cfg.report_rule_bucket)
            else:
                thing.mod.report(reason=reason)
        else:
            thing.report(reason)
    except AttributeError:
        thing.report(reason)


def auto_remove_comment(comment, cfg: Config) -> bool:
    """
    Remove a comment from public view.
    
    When AUTO_REMOVE_ENABLED=true, the bot will:
    1. Remove the comment (hides from public view)
    2. Report it (leaves audit trail of why it was removed)
    3. Send Discord notification for mod review
    
    Removed comments go to the "Removed" queue (not modqueue).
    Mods can approve to restore false positives.
    
    Returns:
        True if removed successfully, False otherwise
    """
    try:
        comment.mod.remove(spam=False)
        logging.info(f"AUTO-REMOVED comment")
        return True
    except Exception as e:
        logging.error(f"Failed to auto-remove comment: {e}")
        return False


def build_report_reason(result: AnalysisResult, include_filter_tag: bool = False) -> str:
    """Build a report reason string from the analysis result.
    
    Reddit report reasons have a ~100 char limit.
    The bot username already shows as the reporter, so no need for prefix.
    """
    max_reason_len = 100
    
    reason = result.reason
    if len(reason) <= max_reason_len:
        return reason
    
    # Truncate cleanly without cutting mid-word
    # Leave room for "..."
    truncated = reason[:max_reason_len - 3]
    last_space = truncated.rfind(' ')
    if last_space > 30:  # Only use space if it's not too far back
        truncated = truncated[:last_space]
    
    return truncated + "..."


# -------------------------------
# Main loop
# -------------------------------

def process_thing(thing, detox_filter: DetoxifyFilter, analyzer: LLMAnalyzer, cfg: Config, subreddit_name: str) -> None:
    """Process a single comment or submission"""
    
    text = get_text_from_thing(thing)
    if not text:
        return
    
    thing_id = thing.fullname
    permalink = f"https://reddit.com{getattr(thing, 'permalink', '')}"
    
    # Get parent context and post title
    context_info = {}
    if hasattr(thing, 'body'):  # It's a comment
        context_info = get_parent_context(thing)
    
    # Check if this is a top-level comment (parent is the submission, not another comment)
    is_top_level = False
    if hasattr(thing, 'parent_id'):
        # parent_id starts with t3_ for submissions, t1_ for comments
        is_top_level = thing.parent_id.startswith('t3_')
    
    # Pre-filter with Detoxify
    should_analyze, detox_score, detox_scores = detox_filter.should_analyze(text, is_top_level=is_top_level)
    
    if not should_analyze:
        # Below threshold - skip LLM analysis
        # Log at INFO level if score was borderline so we can review skips
        if detox_score > cfg.threshold_borderline:
            logging.info(f"SKIP (borderline) | score={detox_score:.2f} | {permalink}")
            logging.info(f"  Text: {text[:200].replace(chr(10), ' ')}{'...' if len(text) > 200 else ''}")
            # Discord notification for borderline skips
            if cfg.discord_webhook:
                notify_discord_borderline_skip(
                    webhook=cfg.discord_webhook,
                    comment_text=text,
                    permalink=permalink,
                    detoxify_score=detox_score,
                    subreddit=subreddit_name
                )
        else:
            logging.debug(f"SKIP | detox={detox_score:.3f} | '{text[:80].replace(chr(10), ' ')}...'")
        return
    
    # Above threshold - send to Groq
    log_text_short = text[:100].replace('\n', ' ')
    logging.info(f"")
    logging.info(f"={'='*60}")
    logging.info(f"SENDING TO GROQ (detox score: {detox_score:.3f})")
    logging.info(f"Comment: \"{log_text_short}{'...' if len(text) > 100 else ''}\"")
    logging.info(f"Link: {permalink}")
    
    # Discord notification for sending to LLM
    if cfg.discord_webhook:
        # Extract trigger reasons from scores dict
        trigger_reasons = detox_scores.get("_trigger_reasons", None)
        notify_discord_llm_analysis(
            webhook=cfg.discord_webhook,
            comment_text=text,
            permalink=permalink,
            detoxify_score=detox_score,
            subreddit=subreddit_name,
            trigger_reasons=trigger_reasons
        )
    
    result = analyzer.analyze(text, subreddit_name, context_info, detox_score, is_top_level, detox_scores)
    
    # Show Groq's verdict and reasoning
    logging.info(f"")
    logging.info(f"GROQ VERDICT: {result.verdict.value}")
    logging.info(f"GROQ REASONING: {result.reason}")
    if result.raw_response:
        logging.debug(f"GROQ RAW RESPONSE: {result.raw_response}")
    
    # Update Discord with verdict
    if cfg.discord_webhook:
        notify_discord_verdict(
            webhook=cfg.discord_webhook,
            verdict=result.verdict.value,
            reason=result.reason,
            permalink=permalink
        )
    
    should_report = result.verdict == Verdict.REPORT
    
    if should_report:
        if cfg.dry_run:
            logging.info(f"ACTION: >>> WOULD REPORT <<< (dry run enabled)")
        else:
            logging.info(f"ACTION: >>> REPORTING <<<")
    else:
        logging.info(f"ACTION: No action needed (benign)")
        # Track benign analyzed comments for prefilter optimization
        # Use _trigger_reasons if available (set by prefilter)
        prefilter_trigger = detox_scores.get("_trigger_reasons", "")
        
        # Fallback: find highest Detoxify score if no trigger reasons
        if not prefilter_trigger and detox_scores:
            numeric_scores = {k: v for k, v in detox_scores.items() 
                           if isinstance(v, (int, float)) and not k.startswith(('openai_', 'perspective_'))}
            if numeric_scores:
                top_label = max(numeric_scores, key=numeric_scores.get)
                prefilter_trigger = f"detoxify:{top_label}={numeric_scores[top_label]:.2f}"
        
        track_benign_analyzed(
            comment_id=thing_id,
            permalink=permalink,
            text=text,
            llm_reason=result.reason,
            detoxify_score=detox_score,
            detoxify_scores=detox_scores,
            is_top_level=is_top_level,
            prefilter_trigger=prefilter_trigger,
            all_ml_scores=detox_scores,  # detox_scores contains all ML scores
            context_info=context_info
        )
    
    logging.info(f"={'='*60}")

    # File report (only if not dry run)
    if cfg.enable_reddit_reports and should_report:
        # Check if we should auto-remove (bot removes + reports for audit trail)
        is_pattern_match = "_trigger_reasons" in detox_scores and "must_escalate" in detox_scores.get("_trigger_reasons", "")
        should_auto_remove, auto_remove_reason = check_auto_remove_consensus(detox_scores, cfg, is_pattern_match)
        
        # Debug logging for auto-remove decision
        logging.info(f"AUTO-REMOVE DEBUG: enabled={cfg.auto_remove_enabled}, require_models={cfg.auto_remove_require_models}, should_auto_remove={should_auto_remove}, reason={auto_remove_reason}")
        
        # Build report reason (no longer need [TOX] tag since we're removing directly)
        reason = build_report_reason(result, include_filter_tag=False)
        
        if cfg.dry_run:
            logging.info(f"ACTION: >>> WOULD REPORT <<< (dry run enabled)")
            if should_auto_remove:
                logging.info(f"ACTION: >>> WOULD AUTO-REMOVE <<< ({auto_remove_reason})")
        else:
            try:
                # If auto-remove enabled and consensus reached, remove then report
                if should_auto_remove:
                    removal_success = auto_remove_comment(thing, cfg)
                    if removal_success:
                        # Report after removal to leave audit trail (won't appear in modqueue, 
                        # but the report reason is attached to the comment)
                        file_report(thing, reason, cfg)
                        logging.info(f"Auto-removed and reported ({auto_remove_reason})")
                    else:
                        # Removal failed, fall back to report only
                        file_report(thing, reason, cfg)
                        logging.warning(f"Auto-remove failed, reported instead")
                else:
                    # Just report, don't remove
                    file_report(thing, reason, cfg)
                
                # Track the reported comment for accuracy measurement
                track_reported_comment(
                    comment_id=thing_id,
                    permalink=permalink,
                    text=text,
                    groq_reason=result.reason,
                    detoxify_score=detox_score,
                    is_top_level=is_top_level,
                    all_ml_scores=detox_scores,
                    context_info=context_info
                )
                
                # Send Discord notification
                if should_auto_remove:
                    # Get author name safely
                    author_name = "unknown"
                    try:
                        if hasattr(thing, 'author') and thing.author:
                            author_name = thing.author.name
                    except:
                        pass
                    
                    # Try Discord Bot first (for editable messages)
                    if cfg.discord_bot_token and cfg.discord_review_channel_id:
                        discord_msg_id = discord_bot_post_review(
                            cfg=cfg,
                            comment_text=text,
                            permalink=permalink,
                            reason=result.reason,
                            scores=detox_scores,
                            auto_remove_reason=auto_remove_reason,
                            author=author_name
                        )
                        # Track for status updates if message was posted
                        if discord_msg_id:
                            add_pending_review(
                                comment_id=thing_id.replace("t1_", "").replace("t3_", ""),
                                discord_message_id=discord_msg_id,
                                permalink=permalink,
                                comment_text=text,
                                reason=result.reason,
                                scores=detox_scores,
                                auto_remove_reason=auto_remove_reason
                            )
                    else:
                        # Fallback to webhook (not editable)
                        notify_discord_auto_remove(
                            webhook=cfg.discord_webhook,
                            comment_text=text,
                            permalink=permalink,
                            reason=result.reason,
                            scores=detox_scores,
                            auto_remove_reason=auto_remove_reason
                        )
                else:
                    notify_discord_report(
                        webhook=cfg.discord_webhook,
                        comment_text=text,
                        permalink=permalink,
                        reason=result.reason,
                        detoxify_score=detox_score
                    )
            except prawcore.exceptions.Forbidden as e:
                logging.error(f"Forbidden when reporting {thing_id}: {e}")
            except prawcore.exceptions.NotFound as e:
                logging.error(f"Item not found {thing_id}: {e}")
            except Exception as e:
                logging.error(f"Report failed for {thing_id}: {e}")


def stream_subreddit(reddit: praw.Reddit, subreddit_name: str, detox_filter: DetoxifyFilter, analyzer: LLMAnalyzer, cfg: Config) -> None:
    """Stream comments and submissions from a subreddit"""
    
    sr = reddit.subreddit(subreddit_name)
    logging.info(f"Starting stream for r/{subreddit_name}")
    
    # Stream comments (this blocks and yields comments as they arrive)
    for comment in sr.stream.comments(skip_existing=True):
        try:
            process_thing(comment, detox_filter, analyzer, cfg, subreddit_name)
        except Exception as e:
            logging.error(f"Error processing {comment.fullname}: {e}")
            continue


# -------------------------------
# Main
# -------------------------------

import threading

def accuracy_check_loop(reddit: praw.Reddit, discord_webhook: str = None, 
                        check_interval_hours: int = 12):
    """Background thread that periodically checks reported comment outcomes"""
    check_interval_sec = check_interval_hours * 3600
    
    while True:
        time.sleep(check_interval_sec)
        try:
            logging.info("Running accuracy check on reported comments...")
            
            # Check outcomes and track false positives
            stats = check_and_track_false_positives(reddit, webhook=discord_webhook)
            
            if stats["checked"] > 0:
                logging.info(
                    f"Accuracy check complete: {stats['checked']} comments checked - "
                    f"{stats['removed']} removed, {stats['approved']} approved (false positives)"
                )
            
            # Get overall stats
            overall = get_accuracy_stats()
            if overall["resolved"] > 0:
                logging.info(
                    f"Overall accuracy: {overall['accuracy_pct']:.1f}% "
                    f"({overall['removed']}/{overall['resolved']} removed) | "
                    f"{overall['pending']} still pending"
                )
            
            # Cleanup old entries
            cleanup_old_tracked(max_age_days=7)
            
        except Exception as e:
            logging.error(f"Accuracy check failed: {e}")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    logging.info("Starting ToxicReportBot v2 (Detoxify + Groq LLM)")

    # Initialize smart pre-filter
    detox_filter = SmartPreFilter(config=cfg)

    # Reddit
    reddit = praw_client(cfg)

    # LLM Analyzer
    analyzer = LLMAnalyzer(
        groq_api_key=cfg.groq_api_key,
        groq_reasoning_effort=cfg.groq_reasoning_effort,
        xai_api_key=cfg.xai_api_key,
        xai_reasoning_effort=cfg.xai_reasoning_effort,
        openai_api_key=cfg.openai_moderation_key,  # Same key used for moderation endpoint
        model=cfg.llm_model,
        guidelines=cfg.moderation_guidelines,
        fallback_chain=cfg.llm_fallback_chain,
        daily_limit=cfg.llm_daily_limit,
        requests_per_minute=cfg.llm_requests_per_minute
    )
    logging.info(f"Using LLM model: {cfg.llm_model} (max {cfg.llm_requests_per_minute} requests/min)")
    if cfg.xai_api_key:
        logging.info(f"x.ai Grok API available for grok-* models (reasoning_effort={cfg.xai_reasoning_effort})")
    if cfg.openai_moderation_key:
        logging.info(f"OpenAI API available for gpt-* models")
    logging.info(f"Fallback chain: {' -> '.join(cfg.llm_fallback_chain)}")
    
    # Discord setup
    if cfg.discord_webhook:
        logging.info(f"Discord notifications enabled (webhook configured)")
        # Send startup notification
        try:
            success = post_discord_embed(
                webhook=cfg.discord_webhook,
                title="🤖 ToxicReportBot Started",
                description=f"Monitoring r/{cfg.subreddits[0]}\nDry run: {cfg.dry_run}",
                color=0x00FF00,  # Green
                fields=[
                    {"name": "LLM Model", "value": cfg.llm_model, "inline": True},
                    {"name": "Detoxify Model", "value": cfg.detoxify_model, "inline": True},
                ]
            )
            if success:
                logging.info("Discord startup notification sent successfully")
            else:
                logging.warning("Discord startup notification failed - check webhook URL")
        except Exception as e:
            logging.error(f"Discord startup notification failed: {e}")
        
        # Post current stats on startup (useful when restarting frequently)
        try:
            # Do live Reddit checks on startup to get accurate stats
            # Rate limit: 0.5s between calls to avoid hitting Reddit API limits
            # This runs once on startup, so a few seconds delay is acceptable
            
            logging.info("Checking pending items for accurate stats (this may take a moment)...")
            
            # Check last 7 days with live Reddit calls (includes 24h items)
            # Use rate limiting to be nice to Reddit API
            accuracy_weekly = get_accuracy_stats(hours=168, reddit=reddit, rate_limit_delay=0.5)
            
            # 24h stats - just filter the already-updated data (no new API calls needed)
            accuracy_daily = get_accuracy_stats(hours=24)
            
            # All-time stats - reload from disk to pick up all updates
            accuracy_alltime = get_accuracy_stats()
            
            startup_stats = {
                "total_processed": detox_filter.total,
                "sent_to_llm": detox_filter.must_escalate + detox_filter.ml_sent,
                "benign": detox_filter.benign_skipped + detox_filter.pattern_skipped,
                "accuracy_daily": accuracy_daily,
                "accuracy_weekly": accuracy_weekly,
                "accuracy_alltime": accuracy_alltime,
                "recent_false_positives": get_recent_false_positives(hours=48, limit=3)
            }
            notify_discord_daily_stats(cfg.discord_webhook, startup_stats)
            logging.info("Posted current stats to Discord on startup")
        except Exception as e:
            logging.error(f"Failed to post startup stats: {e}")
    else:
        logging.info("Discord notifications disabled (no webhook configured)")
    
    # Log Discord Bot configuration
    if cfg.discord_bot_token and cfg.discord_review_channel_id:
        logging.info(f"Discord Bot enabled for editable review notifications (channel: {cfg.discord_review_channel_id})")
    
    # Start accuracy check background thread
    accuracy_thread = threading.Thread(
        target=accuracy_check_loop, 
        args=(reddit, cfg.discord_webhook, 12),  # Check every 12 hours
        daemon=True
    )
    accuracy_thread.start()
    logging.info("Started accuracy tracking (checks every 12 hours)")
    
    # Start pending reviews check thread (for Discord Bot editable messages)
    if cfg.discord_bot_token and cfg.discord_review_channel_id:
        def pending_reviews_loop(reddit_client: praw.Reddit, config: Config):
            """Check pending reviews for mod actions and update Discord messages"""
            while True:
                try:
                    check_pending_reviews(reddit_client, config)
                except Exception as e:
                    logging.error(f"Pending reviews check failed: {e}")
                time.sleep(config.discord_review_check_interval)
        
        reviews_thread = threading.Thread(
            target=pending_reviews_loop,
            args=(reddit, cfg),
            daemon=True
        )
        reviews_thread.start()
        logging.info(f"Started pending reviews tracker (checks every {cfg.discord_review_check_interval}s)")
    else:
        if cfg.enable_discord:
            logging.info("Discord Bot not configured - review notifications will use webhook (not editable)")
    
    # Start daily stats thread
    def daily_stats_loop(discord_webhook: str, detox_filter: DetoxifyFilter, 
                         analyzer: LLMAnalyzer):
        """Post daily stats to Discord"""
        if not discord_webhook:
            return
        
        # Wait until next day at 00:00 UTC, then post daily
        while True:
            # Calculate seconds until midnight UTC
            now = time.gmtime()
            seconds_until_midnight = (24 - now.tm_hour) * 3600 - now.tm_min * 60 - now.tm_sec
            if seconds_until_midnight <= 0:
                seconds_until_midnight += 86400
            
            time.sleep(seconds_until_midnight + 60)  # Wait until just after midnight
            
            try:
                # Gather stats - daily (24h), weekly (7d), and all-time
                accuracy_daily = get_accuracy_stats(hours=24)
                accuracy_weekly = get_accuracy_stats(hours=168)  # 7 days
                accuracy_alltime = get_accuracy_stats()
                
                stats = {
                    "total_processed": detox_filter.total,
                    "sent_to_llm": detox_filter.must_escalate + detox_filter.ml_sent,
                    "benign": detox_filter.benign_skipped + detox_filter.pattern_skipped,
                    "accuracy_daily": accuracy_daily,
                    "accuracy_weekly": accuracy_weekly,
                    "accuracy_alltime": accuracy_alltime,
                    "recent_false_positives": get_recent_false_positives(hours=24, limit=3)
                }
                
                notify_discord_daily_stats(discord_webhook, stats)
                logging.info("Posted daily stats to Discord")
                
            except Exception as e:
                logging.error(f"Daily stats post failed: {e}")
    
    if cfg.discord_webhook:
        stats_thread = threading.Thread(
            target=daily_stats_loop,
            args=(cfg.discord_webhook, detox_filter, analyzer),
            daemon=True
        )
        stats_thread.start()
        logging.info("Started daily Discord stats (posts at midnight UTC)")
    
    # For now, we only support a single subreddit with streaming
    # Multiple subreddits would require threading or asyncio
    if len(cfg.subreddits) > 1:
        logging.warning("Streaming mode only supports one subreddit. Using first one.")
    
    subreddit_name = cfg.subreddits[0]
    logging.info(f"Monitoring r/{subreddit_name} via comment stream")

    while True:
        try:
            stream_subreddit(reddit, subreddit_name, detox_filter, analyzer, cfg)
            
        except prawcore.exceptions.ResponseException as e:
            logging.error("ResponseException: %s", e)
            logging.info(f"Stats - {detox_filter.get_stats()} | {analyzer.get_stats()}")
            time.sleep(10)
            try:
                reddit = praw_client(cfg)
            except Exception:
                time.sleep(20)
        except prawcore.exceptions.RequestException as e:
            logging.error("RequestException: %s", e)
            time.sleep(10)
        except KeyboardInterrupt:
            logging.info("Shutting down by user request.")
            detox_filter.save_stats()  # Persist final stats
            logging.info(f"Final stats - {detox_filter.get_stats()} | {analyzer.get_stats()}")
            # Print final accuracy stats
            overall = get_accuracy_stats()
            if overall["resolved"] > 0:
                logging.info(
                    f"Final accuracy: {overall['accuracy_pct']:.1f}% "
                    f"({overall['removed']}/{overall['resolved']} removed)"
                )
            break
        except Exception as e:
            logging.error("Stream error: %s\n%s", e, traceback.format_exc())
            detox_filter.save_stats()  # Persist stats on error too
            logging.info(f"Stats - {detox_filter.get_stats()} | {analyzer.get_stats()}")
            time.sleep(5)
            # Reconnect and continue
            try:
                reddit = praw_client(cfg)
            except Exception:
                time.sleep(20)


if __name__ == "__main__":
    main()
