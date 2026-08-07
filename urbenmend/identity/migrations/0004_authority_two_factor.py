"""
Per-account 2FA requirement (T1.6 prerequisite, FR-4).

`POST /users/authorities` accepts `"requireTwoFactor": true` [doc: API §6.2] and there was nowhere
to put it — the field is added here, with the login-time enforcement following in T1.7. Storing an
input the spec documents is not the same as implementing the feature; this migration does the
first half only.

Additive, defaulted and nullable-free, so it is backward-compatible in the sense
`database.md` requires: code from the previous deploy — which does not know the column exists —
keeps working, because every existing row gets `False` and nothing reads the value yet.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0003_category_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="require_two_factor",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Whether this account must complete 2FA at login (FR-4). Enforced in T1.7."
                ),
            ),
        ),
    ]
