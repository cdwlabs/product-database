# -*- coding: utf-8 -*-
# Generated by Django 1.9.6 on 2016-05-08 09:55
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('productdb', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='productlist',
            name='products',
        ),
        migrations.RemoveField(
            model_name='product',
            name='json_data',
        ),
        migrations.RemoveField(
            model_name='product',
            name='lists',
        ),
        migrations.DeleteModel(
            name='ProductList',
        ),
    ]
