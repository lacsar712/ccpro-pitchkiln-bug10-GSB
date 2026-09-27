from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kiln", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="firehearth",
            name="tag",
            field=models.CharField(max_length=40, verbose_name="灶牌"),
        ),
    ]
