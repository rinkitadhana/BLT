# Generated by Django 5.1.6 on 2025-03-03 00:50

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("website", "0217_organization_discord_url_organization_element_url_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="Queue",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("message", models.CharField(help_text="Message limited to 140 characters", max_length=140)),
                ("image", models.ImageField(blank=True, null=True, upload_to="queue_images")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                ("launched", models.BooleanField(default=False)),
                ("launched_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-created"],
                "indexes": [models.Index(fields=["created"], name="queue_created_idx")],
            },
        ),
    ]
