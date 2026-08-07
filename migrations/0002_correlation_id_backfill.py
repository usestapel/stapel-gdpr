"""Догоняющая миграция: `0001_initial` правили НА МЕСТЕ, и не один раз.

ЧТО СЛУЧИЛОСЬ. Новые поля моделей дописывали прямо в ``0001_initial``
вместо новой миграции. Чистая установка выглядит исправной: 0001 создаёт
таблицу сразу со всеми колонками, а ``makemigrations --check`` честно
отвечает "No changes detected" — состояние моделей и файла миграции
совпадают. Но любая база, мигрировавшая ДО очередной правки, получает
колонку только в файле: Django видит 0001 применённой и больше к ней не
возвращается.

Замер на стенде ironmemo 07.08.2026: ``stapel_gdpr.tasks.
process_expired_grace_periods`` падал КАЖДЫЙ тик селери с
``UndefinedColumn``. Починив ``correlation_id``, тут же получили
``local_erasure_done``, а следом — отсутствующую ЦЕЛИКОМ таблицу
``gdpr_legalhold``. То есть правок было несколько и разного калибра, и
перечислять их руками значит ловить по одной, прогон за прогоном.

ПОЭТОМУ МИГРАЦИЯ ОБЩАЯ. Она сверяет КАЖДУЮ модель приложения с реальной
таблицей и добавляет всё, чего в базе нет. Установки сейчас в разных
состояниях (кто-то мигрировал раньше, кто-то позже), и единственный
источник правды о конкретной базе — сама база.

Это уборка за уже случившимся, а НЕ приём. Править применённую миграцию
нельзя: следующий такой случай снова доедет до продакшена молча.
"""
from django.db import migrations


def add_missing_columns(apps, schema_editor):
    connection = schema_editor.connection
    app = apps.get_app_config("gdpr")
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        for model in app.get_models():
            table = model._meta.db_table
            if table not in tables:
                # В `0001` дописывали и ЦЕЛЫЕ модели: на стенде ironmemo так
                # не оказалось таблицы `gdpr_legalhold`. Создаём — иначе
                # догоняющая миграция чинила бы половину и снова уходила бы
                # в отказ на следующем запросе.
                print(f"  gdpr: таблицы {table} не было — создаю")
                schema_editor.create_model(model)
                continue
            present = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, table
                )
            }
            for field in model._meta.local_concrete_fields:
                if field.column in present:
                    continue
                # ``schema_editor`` вне блока `with cursor` не нужен: он
                # открывает свой. Печатаем — молчаливое исправление схемы
                # хуже самого расхождения.
                print(f"  gdpr: {table}.{field.column} отсутствовал — добавляю")
                schema_editor.add_field(model, field)


def noop_reverse(apps, schema_editor):
    """Назад ничего не снимаем.

    Колонки могли существовать до этой миграции — установки в разных
    состояниях, — и снести их на откате значило бы отобрать данные у того,
    кто получил их не через нас.
    """


class Migration(migrations.Migration):
    dependencies = [("gdpr", "0001_initial")]

    operations = [
        # `state_operations` пуст намеренно: состояние моделей уже описано
        # в `0001_initial` (её и правили). Трогаем ТОЛЬКО базу, и только
        # там, где она отстала.
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(add_missing_columns, noop_reverse)],
            state_operations=[],
        ),
    ]
