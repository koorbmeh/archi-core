"""Implements secure Google Sheets API integration using service account authentication to read and analyze financial data from user-shared sheets, generating summaries, detecting anomalies, computing trends, and integrating with Discord commands and the event loop for periodic bill reviews."""

import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from capabilities.discord_notifier import notify_async
from capabilities.event_loop import EventLoop, PeriodicTask
from capabilities.personal_profile_manager import get_manager as get_profile_manager
from src.kernel.capability_registry import Capability, CapabilityRegistry


def load_service_account_credentials() -> dict:
    creds_json = os.environ.get('GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON')
    if not creds_json:
        raise ValueError('GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON env var required')
    return json.loads(creds_json)


def get_gspread_client() -> gspread.Client:
    info