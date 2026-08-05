"""Baseline schema: enable PostGIS, then create the custom user model (T0.4 / T0.10).

⚠️ `CreateExtension("postgis")` is the FIRST operation in the FIRST migration, deliberately
[doc: Arch §2.3, DevOps §7, Plan T0.4]. Every geometry column added later (Report location,
POI location — `PointField(geography=True, srid=4326)`) needs the extension to already exist,
and a migration cannot create a `geography` column in a database that lacks it.

It lives here rather than in `geo`/`reporting` — the apps that actually own geometry — because
those migrations do not yet exist and Django orders by the dependency graph, not by app name.
`identity.0001` is the earliest project-owned node in that graph: `AUTH_USER_MODEL` points at
it, so `contrib.admin` and every model with an FK to the user depend on it transitively.

⚠️ A future geometry-bearing app must still name this migration in its `dependencies` if it
has no other path to it — do not assume alphabetical or app-registry order will save you.

Two operational notes [doc: DevOps §7]:
- `CREATE EXTENSION` needs a role with sufficient privilege. Django emits `IF NOT EXISTS`, so
  where a DBA has pre-created the extension this is a harmless no-op rather than a failure.
- The reverse operation DROPs the extension. Rolling this migration back on a database that
  already holds geometry data would be destructive; migrations run forward as a pre-deploy Job.

Hand-edited after `makemigrations` — a generated migration is a draft to review, not an
artifact to trust [doc: DevOps §7]. Safe to edit because it has never been applied to a shared
environment; once it has, it is frozen.
"""

import uuid

import django.core.validators
import django.utils.timezone
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        # ⚠️ Must precede every geometry column in the project. Do not reorder.
        CreateExtension("postgis"),
        migrations.CreateModel(
            name='User',
            fields=[
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('email', models.EmailField(blank=True, max_length=254, null=True, unique=True, verbose_name='email address')),
                ('phone', models.CharField(blank=True, max_length=16, null=True, unique=True, validators=[django.core.validators.RegexValidator(message='Enter the phone number in E.164 format, for example +8801712345678.', regex='^\\+[1-9]\\d{7,14}$')], verbose_name='phone number')),
                ('email_verified_at', models.DateTimeField(blank=True, null=True)),
                ('phone_verified_at', models.DateTimeField(blank=True, null=True)),
                ('role', models.CharField(choices=[('citizen', 'Citizen'), ('authority', 'Authority'), ('admin', 'Admin')], default='citizen', max_length=16)),
                ('status', models.CharField(choices=[('registered', 'Registered (unverified)'), ('verified', 'Verified'), ('active', 'Active'), ('suspended', 'Suspended'), ('deprovisioned', 'Deprovisioned'), ('deleted', 'Deleted (PII anonymized)')], default='registered', max_length=16)),
                ('preferred_language', models.CharField(choices=[('en', 'English'), ('bn', 'Bangla')], default='en', max_length=8)),
                ('is_staff', models.BooleanField(default=False, help_text='Whether this user may sign in to the Django admin site (FR-30/31).', verbose_name='staff status')),
                ('date_joined', models.DateTimeField(default=django.utils.timezone.now)),
                ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'verbose_name': 'user',
                'verbose_name_plural': 'users',
                'indexes': [models.Index(fields=['role', 'status'], name='identity_user_role_status_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(('email__isnull', False), ('phone__isnull', False), ('status', 'deleted'), _connector='OR'), name='identity_user_has_contact_or_anonymized')],
            },
        ),
    ]
