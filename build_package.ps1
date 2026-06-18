param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+(\.\d+)*$')]
    [string]$Version,

    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dist = Join-Path $Root "dist"
$Backup = Join-Path $Dist "backup"
$PackageName = "Elemer_checker_package_v.$Version"
$PackageDir = Join-Path $Dist $PackageName
$ZipPath = Join-Path $Dist "$PackageName.zip"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Move-ToBackup {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $Name = Split-Path -Leaf $Path
    $Target = Join-Path $Backup $Name
    if (Test-Path -LiteralPath $Target) {
        $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $Target = Join-Path $Backup "$Name.$Stamp"
    }
    try {
        Move-Item -LiteralPath $Path -Destination $Target -Force -ErrorAction Stop
    } catch {
        $Item = Get-Item -LiteralPath $Path -Force
        if ($Item.PSIsContainer) {
            $PostgresData = Get-ChildItem -LiteralPath $Path -Directory -Recurse -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq "postgres-data" } |
                Select-Object -First 1
            if ($PostgresData) {
                Write-Warning "Cannot move '$Path' because it contains Docker runtime data. Leaving it in dist to avoid deleting PostgreSQL data. Stop Docker and move/delete it manually if you need a perfectly clean dist folder."
                return
            }
        }
        throw
    }
}

function Stop-DistComposeContainers {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return
    }

    $ContainerIds = @(docker ps -a --filter "name=elemer" --format "{{.ID}}" 2>$null)
    if (-not $ContainerIds) {
        return
    }

    $WorkingDirs = New-Object "System.Collections.Generic.HashSet[string]"
    foreach ($ContainerId in $ContainerIds) {
        $Inspect = docker inspect $ContainerId 2>$null | ConvertFrom-Json
        if (-not $Inspect) {
            continue
        }
        $WorkingDir = $Inspect[0].Config.Labels.'com.docker.compose.project.working_dir'
        if ([string]::IsNullOrWhiteSpace($WorkingDir) -or $WorkingDir -eq "<no value>") {
            continue
        }

        try {
            $ResolvedWorkingDir = (Resolve-Path -LiteralPath $WorkingDir -ErrorAction Stop).Path
        } catch {
            $ResolvedWorkingDir = $WorkingDir
        }

        if ($ResolvedWorkingDir.StartsWith($Dist, [System.StringComparison]::OrdinalIgnoreCase)) {
            [void]$WorkingDirs.Add($WorkingDir)
        }
    }

    foreach ($WorkingDir in $WorkingDirs) {
        Write-Host "Stopping Docker Compose project from dist: $WorkingDir" -ForegroundColor Yellow
        docker compose --project-directory $WorkingDir stop
        docker compose --project-directory $WorkingDir rm -f
    }
}

function Copy-RequiredFile {
    param([string]$RelativePath)

    $Source = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Required file not found: $RelativePath"
    }
    Copy-Item -LiteralPath $Source -Destination $PackageDir -Force
}

function New-InstallDocs {
    $ReadmeMd = Join-Path $PackageDir "README_INSTALL.md"
    $ReadmeTxt = Join-Path $PackageDir "README_INSTALL.txt"

    @"
# Установка Elemer Checker v$Version

## Что нужно установить

- Windows 10/11.
- Docker Desktop.
- Архив ``$PackageName.zip``.

## Порядок установки

1. Распаковать архив в отдельную папку, например ``C:\Elemer_checker``.
2. Установить и запустить Docker Desktop.
3. В папке программы запустить ``start_services.bat``.
4. Дождаться запуска PostgreSQL и Grafana.
5. Запустить ``ElemerChecker.exe`` для работы с реальным оборудованием или ``ElemerCheckerModeler.exe`` для моделирования.
6. Открывать дашборды кнопками из блока ``Окна Grafana`` в программе.

## Grafana

- Адрес: ``http://localhost:3001``.
- Администратор: логин ``admin``, пароль задан в ``.env``.
- Просмотр дашбордов: логин ``user``, пароль ``user``.
- Редактирование цветов, типов линий и override графиков: логин ``test``, пароль ``test``.

## Командные файлы

- ``start_services.bat`` - запуск PostgreSQL и Grafana. Перед запуском автоматически удаляет только остановленные старые контейнеры ``elemer-postgres`` и ``elemer-grafana``.
- ``stop_services.bat`` - остановка PostgreSQL и Grafana.
- ``run_main.bat`` - запуск основной программы.
- ``run_modeler.bat`` - запуск моделера.

## Данные

- Папка ``postgres-data`` не поставляется в архиве. Она создается автоматически при первом запуске PostgreSQL.
- Дашборды Grafana поставляются в ``grafana/dashboards``, ``grafana-data/dashboards`` и ``grafana-data/grafana.db``.
- Runtime-папки Grafana, логи и измерения в архив не включаются.
"@ | Set-Content -LiteralPath $ReadmeMd -Encoding UTF8

    @"
Установка Elemer Checker v$Version

Что нужно установить

- Windows 10/11.
- Docker Desktop.
- Архив $PackageName.zip.

Порядок установки

1. Распаковать архив в отдельную папку, например C:\Elemer_checker.
2. Установить и запустить Docker Desktop.
3. В папке программы запустить start_services.bat.
4. Дождаться запуска PostgreSQL и Grafana.
5. Запустить ElemerChecker.exe для работы с реальным оборудованием или ElemerCheckerModeler.exe для моделирования.
6. Открывать дашборды кнопками из блока "Окна Grafana" в программе.

Grafana

- Адрес: http://localhost:3001.
- Администратор: логин admin, пароль задан в .env.
- Просмотр дашбордов: логин user, пароль user.
- Редактирование цветов, типов линий и override графиков: логин test, пароль test.

Командные файлы

- start_services.bat - запуск PostgreSQL и Grafana. Перед запуском автоматически удаляет только остановленные старые контейнеры elemer-postgres и elemer-grafana.
- stop_services.bat - остановка PostgreSQL и Grafana.
- run_main.bat - запуск основной программы.
- run_modeler.bat - запуск моделера.

Данные

- Папка postgres-data не поставляется в архиве. Она создается автоматически при первом запуске PostgreSQL.
- Дашборды Grafana поставляются в grafana/dashboards, grafana-data/dashboards и grafana-data/grafana.db.
- Runtime-папки Grafana, логи и измерения в архив не включаются.
"@ | Set-Content -LiteralPath $ReadmeTxt -Encoding UTF8
}

function New-UserGuide {
    $GuideTxt = Join-Path $PackageDir "Elemer_Checker_User_Guide.txt"
    $GuideDocx = Join-Path $PackageDir "Elemer_Checker_User_Guide.docx"

    @"
Инструкция по работе с Elemer Checker v$Version

Elemer Checker предназначен для опроса модулей TM5104, записи измерений в PostgreSQL/TimescaleDB и просмотра данных в Grafana. Elemer Checker Modeler используется для проверки интерфейса и дашбордов без подключения оборудования.

1. Запуск

Перед запуском программы запустите Docker Desktop и выполните start_services.bat. После запуска контейнеров PostgreSQL доступен на localhost:55432, Grafana доступна на http://localhost:3001.

Для работы с реальным оборудованием используйте ElemerChecker.exe. Для моделирования данных используйте ElemerCheckerModeler.exe.

2. Основное окно

В основном окне задаются COM-порт, скорость, стоп-биты, адреса устройств Элемер и параметры автоматического опроса. Период автоопроса выбирается из списка и применяется сразу после выбора.

Кнопка запуска автоопроса начинает циклическое чтение активных каналов. Полученные значения сразу записываются в базу данных и используются дашбордами Grafana.

3. Состояние датчиков

Каждый датчик отображает название, последнее значение и знак тренда. Стрелка вверх означает рост, стрелка вниз означает снижение, знак равно означает отсутствие значимого изменения.

Тренд рассчитывается после накопления не менее 10 значений: сравнивается среднее последних 5 значений со средним предыдущих 5 значений.

4. Настройки каналов

В инженерном меню настраиваются активность канала, название датчика, тип датчика, пределы Tmin/Tmax/Twar/Tcrit/Temerg, коэффициенты пересчета и эмиссивность. Кнопка сохранения применяет настройки и записывает их в channel_settings.json.

5. Окна Grafana

В блоке Окна Grafana расположены кнопки перехода к дашбордам: Схема ТД, Схема ДТП, Графики ТД, Графики ДТП, Скорости ТД, Скорости ДТП, а также кнопка открытия всех графиков.

Все ссылки открываются с автообновлением 1 секунда и периодом данных за последние 15 минут.

6. Пользователи Grafana

Для просмотра дашбордов используйте пользователя user с паролем user.

Для редактирования цветов графиков, типа линий и overrides используйте пользователя test с паролем test. Этот пользователь имеет права Editor и предназначен для настройки внешнего вида графиков.

Администратор Grafana: логин admin, пароль указан в файле .env.

7. Сборка пакета

Пакет собирается скриптом build_package.ps1. Номер версии передается параметром Version, например: powershell -ExecutionPolicy Bypass -File .\build_package.ps1 -Version $Version.
"@ | Set-Content -LiteralPath $GuideTxt -Encoding UTF8

    $Python = @'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from html import escape
import sys

src = Path(sys.argv[1])
out = Path(sys.argv[2])
lines = src.read_text(encoding="utf-8-sig").splitlines()

def run(text, bold=False, size=None):
    props = []
    if bold:
        props.append("<w:b/>")
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    rpr = "<w:rPr>" + "".join(props) + "</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'

def para(text, style):
    if not text:
        return "<w:p/>"
    if style == "title":
        return f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr>{run(text, True, 32)}</w:p>'
    if style == "heading":
        return f"<w:p>{run(text, True, 26)}</w:p>"
    return f"<w:p>{run(text, False, 22)}</w:p>"

body = []
for index, line in enumerate(lines):
    style = "normal"
    if index == 0:
        style = "title"
    elif line and len(line) <= 45 and line[0].isdigit() and ". " in line[:4]:
        style = "heading"
    body.append(para(line, style))

document = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    f'<w:body>{"".join(body)}'
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/>'
    '</w:sectPr></w:body></w:document>'
)
content = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
rels = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>'
)
doc_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'

with ZipFile(out, "w", ZIP_DEFLATED) as zf:
    zf.writestr("[Content_Types].xml", content)
    zf.writestr("_rels/.rels", rels)
    zf.writestr("word/document.xml", document)
    zf.writestr("word/_rels/document.xml.rels", doc_rels)
'@

    $Python | python - $GuideTxt $GuideDocx
}

function Test-FileHasNoLiteralNewlines {
    param([string]$Path)

    $Bytes = [System.IO.File]::ReadAllBytes($Path)
    $Text = [System.Text.Encoding]::ASCII.GetString($Bytes)
    if ($Text.Contains('`r`n')) {
        throw "File contains literal backtick newlines: $Path"
    }
    if (-not $Text.Contains("`n")) {
        throw "File has no line breaks: $Path"
    }
}

Set-Location -LiteralPath $Root
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
New-Item -ItemType Directory -Force -Path $Backup | Out-Null

Write-Step "Stopping old package containers"
Stop-DistComposeContainers

Write-Step "Cleaning dist"
Get-ChildItem -LiteralPath $Dist -Force | Where-Object { $_.Name -ne "backup" } | ForEach-Object {
    Move-ToBackup $_.FullName
}

if (-not $SkipPyInstaller) {
    Write-Step "Building executables"
    python -m PyInstaller ElemerChecker.spec --noconfirm
    python -m PyInstaller ElemerCheckerModeler.spec --noconfirm
} else {
    Write-Step "Skipping PyInstaller build"
}

Write-Step "Creating package folder"
if (Test-Path -LiteralPath $PackageDir) {
    Remove-Item -LiteralPath $PackageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $PackageDir | Out-Null

$RootFiles = @(
    ".env",
    ".env.example",
    "channel_settings.json",
    "channel_settings_true.xlsx",
    "channel_settings_true_test.xlsx",
    "docker-compose.yml",
    "element_checker.py",
    "element_checker_modeler.py",
    "README.md",
    "requirements.txt",
    "start_services.bat",
    "stop_services.bat",
    "run_main.bat",
    "run_modeler.bat"
)

foreach ($File in $RootFiles) {
    Copy-RequiredFile $File
}

Copy-Item -LiteralPath (Join-Path $Dist "ElemerChecker.exe") -Destination $PackageDir -Force
Copy-Item -LiteralPath (Join-Path $Dist "ElemerCheckerModeler.exe") -Destination $PackageDir -Force

foreach ($Bat in @("start_services.bat", "stop_services.bat", "run_main.bat", "run_modeler.bat")) {
    Test-FileHasNoLiteralNewlines (Join-Path $PackageDir $Bat)
}

Write-Step "Creating documentation"
New-InstallDocs
New-UserGuide

Write-Step "Copying Grafana files"
Copy-Item -LiteralPath (Join-Path $Root "grafana") -Destination (Join-Path $PackageDir "grafana") -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $PackageDir "grafana-data") | Out-Null
Copy-Item -LiteralPath (Join-Path $Root "grafana-data\dashboards") -Destination (Join-Path $PackageDir "grafana-data\dashboards") -Recurse -Force

Write-Step "Copying Grafana database snapshot"
$DbSource = Join-Path $Root "grafana-data\grafana.db"
$DbTarget = Join-Path $PackageDir "grafana-data\grafana.db"
$DbBackupPython = @'
import sqlite3
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.parent.mkdir(parents=True, exist_ok=True)
source = sqlite3.connect(src)
target = sqlite3.connect(dst)
with target:
    source.backup(target)
target.close()
source.close()
print(dst, dst.stat().st_size)
'@
$DbBackupPython | python - $DbSource $DbTarget

New-Item -ItemType Directory -Force -Path (Join-Path $PackageDir "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PackageDir "measurements") | Out-Null

Write-Step "Creating zip"
$ZipPython = @'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import sys

package_dir = Path(sys.argv[1])
zip_path = Path(sys.argv[2])
root_name = package_dir.name

if zip_path.exists():
    zip_path.unlink()

def should_include(path: Path) -> bool:
    rel = path.relative_to(package_dir)
    parts = rel.parts
    if not parts:
        return False
    if parts[0] == "postgres-data":
        return False
    if parts[0] == "grafana-data":
        return rel == Path("grafana-data/grafana.db") or (len(parts) >= 2 and parts[1] == "dashboards")
    return True

with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
    for path in package_dir.rglob("*"):
        if path.is_dir() or not should_include(path):
            continue
        archive_name = (Path(root_name) / path.relative_to(package_dir)).as_posix()
        zf.write(path, archive_name)

with ZipFile(zip_path) as zf:
    bad = zf.testzip()
    if bad:
        raise SystemExit(f"Zip test failed at {bad}")
    names = zf.namelist()
    has_postgres_data = any("/postgres-data/" in name.replace("\\", "/") for name in names)
    if has_postgres_data:
        raise SystemExit("postgres-data unexpectedly included in zip")
    has_grafana_db = any(name.replace("\\", "/").endswith("/grafana-data/grafana.db") for name in names)
    if not has_grafana_db:
        raise SystemExit("grafana-data/grafana.db is missing from zip")
    grafana_dashboards = [name for name in names if "/grafana/dashboards/" in name.replace("\\", "/") and name.endswith(".json")]
    grafana_data_dashboards = [name for name in names if "/grafana-data/dashboards/" in name.replace("\\", "/") and name.endswith(".json")]
    print("zip_test OK")
    print("entries", len(names))
    print("grafana_dashboards", len(grafana_dashboards))
    print("grafana_data_dashboards", len(grafana_data_dashboards))
    print("postgres_data_present", has_postgres_data)
    print("grafana_db_present", has_grafana_db)
print(zip_path, zip_path.stat().st_size)
'@
$ZipPython | python - $PackageDir $ZipPath

Write-Step "Moving build leftovers to backup"
$BuildBackup = Join-Path $Backup ("build_v$Version" + "_" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $BuildBackup | Out-Null
foreach ($Name in @($PackageName, "ElemerChecker.exe", "ElemerCheckerModeler.exe")) {
    $Path = Join-Path $Dist $Name
    if (Test-Path -LiteralPath $Path) {
        Move-Item -LiteralPath $Path -Destination $BuildBackup
    }
}

Write-Step "Done"
Write-Host "Created: $ZipPath" -ForegroundColor Green
