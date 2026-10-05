<?php
// Общие функции приёма заявок с сайта. Работает на обычном хостинге reg.ru (PHP 7.4+), без базы данных:
// заявки лежат в файле api/data/leads.php, закрытом от скачивания.
declare(strict_types=1);

// Первая строка файлов данных: если веб-сервер всё же отдаст файл по прямой ссылке, PHP выполнит exit.
const DATA_GUARD = "<?php exit; ?>\n";
const RATE_LIMIT_PER_HOUR = 5;
const MAX_LEADS_PER_REPLY = 100;

function cfg(): array
{
    static $cfg = null;
    if ($cfg === null) {
        $file = __DIR__ . '/config.php';  // пишется при выкладке, в репозитории его нет
        $cfg = is_file($file) ? (array)(require $file) : [];
    }
    return $cfg;
}

function json_out(int $code, array $body): void
{
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    echo json_encode($body, JSON_UNESCAPED_UNICODE);
    exit;
}

function data_dir(): string
{
    $dir = __DIR__ . '/data';
    if (!is_dir($dir) && !mkdir($dir, 0700, true) && !is_dir($dir)) {
        json_out(500, ['ok' => false, 'error' => 'storage']);
    }
    if (!is_file($dir . '/.htaccess')) {
        file_put_contents($dir . '/.htaccess',
            "<IfModule mod_authz_core.c>\nRequire all denied\n</IfModule>\n<IfModule !mod_authz_core.c>\nDeny from all\n</IfModule>\n");
    }
    if (!is_file($dir . '/index.html')) {
        file_put_contents($dir . '/index.html', '');
    }
    return $dir;
}

/** Строка из запроса: без управляющих символов, обрезанная до $max символов. */
function clean($value, int $max): string
{
    if (!is_scalar($value)) {
        return '';
    }
    $s = preg_replace('/[\x00-\x1F\x7F]+/u', ' ', (string)$value);
    $s = trim((string)$s);
    if (function_exists('mb_substr')) {
        return mb_substr($s, 0, $max, 'UTF-8');
    }
    // Без mbstring режем по символам UTF-8 регуляркой, чтобы не разрезать букву пополам
    return preg_match('/^.{0,' . $max . '}/us', $s, $m) ? $m[0] : '';
}

/** Открывает файл данных под эксклюзивной блокировкой и возвращает [handle, строки JSON без защитной строки]. */
function open_locked(string $file): array
{
    $fh = fopen($file, 'c+');
    if ($fh === false || !flock($fh, LOCK_EX)) {
        json_out(500, ['ok' => false, 'error' => 'storage']);
    }
    $raw = stream_get_contents($fh);
    $lines = [];
    foreach (explode("\n", (string)$raw) as $line) {
        $line = trim($line);
        if ($line === '' || strpos($line, '<?php') === 0) {
            continue;
        }
        $row = json_decode($line, true);
        if (is_array($row)) {
            $lines[] = $row;
        }
    }
    return [$fh, $lines];
}

function close_locked($fh): void
{
    fflush($fh);
    flock($fh, LOCK_UN);
    fclose($fh);
}

/** Не больше RATE_LIMIT_PER_HOUR заявок в час с одного адреса. Храним только хеш адреса. */
function rate_ok(string $ip): bool
{
    $key = hash('sha256', $ip . '|' . (string)(cfg()['salt'] ?? 'car-leads'));
    $file = data_dir() . '/rate.php';
    [$fh, $rows] = open_locked($file);
    $now = time();
    $fresh = [];
    $mine = 0;
    foreach ($rows as $r) {
        if (($r['t'] ?? 0) > $now - 3600) {
            $fresh[] = $r;
            if (($r['k'] ?? '') === $key) {
                $mine++;
            }
        }
    }
    $ok = $mine < RATE_LIMIT_PER_HOUR;
    if ($ok) {
        $fresh[] = ['k' => $key, 't' => $now];
    }
    $body = DATA_GUARD;
    foreach ($fresh as $r) {
        $body .= json_encode($r) . "\n";
    }
    ftruncate($fh, 0);
    rewind($fh);
    fwrite($fh, $body);
    close_locked($fh);
    return $ok;
}

/** Дописывает заявку и возвращает её номер (следующий после последнего). */
function append_lead(array $lead): int
{
    $file = data_dir() . '/leads.php';
    [$fh, $rows] = open_locked($file);
    $last = 0;
    foreach ($rows as $r) {
        $last = max($last, (int)($r['id'] ?? 0));
    }
    $lead = ['id' => $last + 1] + $lead;
    fseek($fh, 0, SEEK_END);
    if (ftell($fh) === 0) {
        fwrite($fh, DATA_GUARD);
    }
    fwrite($fh, json_encode($lead, JSON_UNESCAPED_UNICODE) . "\n");
    close_locked($fh);
    return $lead['id'];
}

/** Заявки с номером больше $after (не больше MAX_LEADS_PER_REPLY) и номер последней заявки. */
function leads_after(int $after): array
{
    $file = data_dir() . '/leads.php';
    if (!is_file($file)) {
        return [[], 0];
    }
    [$fh, $rows] = open_locked($file);
    close_locked($fh);
    $out = [];
    $last = 0;
    foreach ($rows as $r) {
        $id = (int)($r['id'] ?? 0);
        $last = max($last, $id);
        if ($id > $after && count($out) < MAX_LEADS_PER_REPLY) {
            $out[] = $r;
        }
    }
    return [$out, $last];
}
