"""Выполнение сгенерированного кода в песочнице bubblewrap.

Почему bubblewrap (а не Podman/systemd-run):
    • уже установлен в системе (проверено), не требует root;
    • изоляция на уровне процессов ядра (namespaces) без демонов и образов;
    • стартует за миллисекунды, что важно для интерактивного ассистента;
    • Podman не установлен (пришлось бы ставить + качать образы);
    • systemd-run --user даёт слабую изоляцию без extra-опций и зависит
      от user-сессии systemd.

Изоляция скрипта:
    --ro-bind / /      корневая ФС только для чтения — скрипт не может
                       изменить ничего на хосте;
    --tmpfs /tmp       писать можно только во временную ФС (внутри песочницы);
    --unshare-all      изолированные сеть/pid/ipc/uts/cgroup namespace;
    --clearenv         окружение хоста не наследуется (секреты не видны);
    --die-with-parent  если ассистент умирает — песочница умирает с ним;
    rlimit-ы           лимит памяти (RLIMIT_AS), CPU-времени (RLIMIT_CPU)
                       и размера создаваемых файлов (RLIMIT_FSIZE).

Любой код, присланный облаком, выполняется ТОЛЬКО здесь — на хосте
он не исполняется никогда.
"""

import os
import resource
import shutil
import subprocess
import tempfile

from jarvis import logger


def _set_rlimits(memory_mb, time_sec):
    """Устанавливает лимиты процесса песочницы (понижение не требует root)."""
    if memory_mb:
        resource.setrlimit(resource.RLIMIT_AS,
                           (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024))
    if time_sec:
        resource.setrlimit(resource.RLIMIT_CPU, (time_sec, time_sec + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))


def run_code(code, policy):
    """Запускает Python-код в bubblewrap.

    Возвращает (ok, message) — message включает вывод и код возврата,
    либо понятную ошибку (нет bwrap / таймаут / лимит памяти).
    """
    if not code or not code.strip():
        return False, 'Пустой код.'
    if not shutil.which('bwrap'):
        return False, ('bubblewrap (bwrap) не установлен — выполнение кода '
                       'в песочнице невозможно. Без песочницы код не запускается.')

    max_chars = int(policy.sandbox.get('code_max_chars', 20000))
    if len(code) > max_chars:
        return False, f'Код слишком большой ({len(code)} > {max_chars} символов).'

    time_limit = int(policy.sandbox.get('time_limit_sec', 30))
    memory_mb = int(policy.sandbox.get('memory_limit_mb', 512))
    output_max = int(policy.sandbox.get('output_max_bytes', 65536))

    tmpdir = tempfile.mkdtemp(prefix='jarvis-sandbox-')
    script = os.path.join(tmpdir, 'main.py')
    try:
        with open(script, 'w', encoding='utf-8') as f:
            f.write(code)

        args = [
            'bwrap',
            '--ro-bind', '/', '/',          # ФС хоста только для чтения
            '--tmpfs', '/tmp',              # временные файлы — внутри песочницы
            '--dev', '/dev',
            '--proc', '/proc',
            '--unshare-all',                # сеть, pid, ipc, uts, cgroup
            '--clearenv',                   # не наследовать окружение хоста
            '--setenv', 'HOME', '/tmp',
            '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'LANG', 'C.UTF-8',
            '--die-with-parent',
            '--bind', tmpdir, '/app',       # только каталог со скриптом
            '/usr/bin/python3', '/app/main.py',
        ]

        try:
            r = subprocess.run(
                args, capture_output=True, timeout=time_limit,
                preexec_fn=lambda: _set_rlimits(memory_mb, time_limit))
        except subprocess.TimeoutExpired:
            return False, (f'Код превысил лимит времени ({time_limit} с) '
                           'и был остановлен песочницей.')
        except subprocess.SubprocessError as exc:
            return False, f'Ошибка запуска песочницы: {exc}'

        stdout = (r.stdout or b'').decode('utf-8', errors='replace')
        stderr = (r.stderr or b'').decode('utf-8', errors='replace')
        truncated = False
        if len(stdout) > output_max:
            stdout = stdout[:output_max] + '\n...[вывод обрезан]'
            truncated = True

        message = stdout.strip() or stderr.strip() or '(пустой вывод)'
        if r.returncode == 0 and not truncated:
            return True, f'Код выполнен в песочнице (exit 0).\n{message}'
        if r.returncode == 0:
            return True, f'Код выполнен (exit 0), вывод обрезан.\n{message}'
        return False, (f'Код завершился с кодом {r.returncode} '
                       f'в песочнице.\n{message}')
    except OSError as exc:
        logger.get_logger().event('sandbox_error', error=str(exc))
        return False, f'Ошибка песочницы: {exc}'
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)