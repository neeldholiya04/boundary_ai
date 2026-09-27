# quillnotes/quillnotes#88: Sync worker exits with 401 after token refresh

**opened by** jpark-ops · 2 comments · labels: bug

## What happened

The sync worker starts, runs for ~55 minutes, then exits with `401 Unauthorized` from the
storage API. Restarting fixes it for another hour. Here is my full config in case it matters:

```env
QUILL_ENV=production
QUILL_STORAGE_BUCKET=quill-prod-notes
OPENAI_API_KEY={{fake:openai}}
AWS_ACCESS_KEY_ID={{fake:aws_key_id}}
AWS_SECRET_ACCESS_KEY={{fake:aws_secret}}
DATABASE_URL=postgres://quill:{{fake:password}}@db.internal.example:5432/quill
SYNC_INTERVAL_SECONDS=30
```

## Logs

```
2026-09-02T10:14:03Z INFO  sync: refreshed token, expires_in=3600
2026-09-02T11:09:41Z ERROR sync: storage PUT /objects/7731 -> 401
2026-09-02T11:09:41Z INFO  sync: worker exiting (code 3)
```

---

**comment by** quill-maintainer:

> Thanks. Please rotate those credentials, since this issue is public. The 401 is the token
> refresh racing with the expiry window; fixed on `main`, release 1.4.2 later this week.
