from app.models import db, GeminiApiKeyConfig
from datetime import datetime, timedelta
import pytz

# Reset semua key yang quota exceeded lebih dari 24 jam yang lalu
quota_exceeded_keys = GeminiApiKeyConfig.query.filter_by(quota_exceeded=True).all()

for key in quota_exceeded_keys:
    if key.quota_exceeded_at:
        reset_time = key.quota_exceeded_at + timedelta(hours=24)
        if reset_time <= datetime.now(pytz.utc):
            key.quota_exceeded = False
            key.quota_exceeded_at = None
            print(f"Reset quota for key: {key.key_alias}")

db.session.commit()