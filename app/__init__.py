"""Strava strength-activity merger service.

Webhook-driven FastAPI service that merges strength activities synced from
Garmin (heart-rate stream) and Hevy (exercise/set data) into a single Strava
activity, then optionally deletes the two originals.
"""

__version__ = "0.1.0"
