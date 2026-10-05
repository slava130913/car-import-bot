<?php
// Общие функции приёма заявок с сайта. Работает на обычном хостинге reg.ru (PHP 7.4+), без базы данных:
// заявки лежат в файле api/data/leads.php, закрытом от скачивания.
declare(strict_types=1);

// Первая строка файлов данных: если веб-сервер всё же отдаст файл по прямой ссылке, PHP выполнит exit.
const DATA_GUARD = "<?php exit; ?>\n";
const RATE_LIMIT_PER_HOUR = 5;
const MAX_LEADS_PER_REPLY = 100;
const KEEP_DAYS = 365;  // столько хранятся заявки на хостинге (политика: не дольше 12 месяцев)

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

/** Переписывает файл данных целиком. false, если запись не удалась (например, кончилось место). */
function write_rows($fh, array $rows): bool
{
    $body = DATA_GUARD;
    foreach ($rows as $r) {
        $line = json_encode($r, JSON_UNESCAPED_UNICODE);
        if ($line === false) {
            return false;
        }
        $body .= $line . "\n";
    }
    return ftruncate($fh, 0) && rewind($fh) && fwrite($fh, $body) === strlen($body) && fflush($fh);
}

/** Записи о частоте заявок старше часа больше не нужны: удаляем. Вызывается и при опросе ботом раз в минуту,
 *  поэтому хеш адреса хранится около часа, даже если новых заявок нет. */
function rate_rows_fresh(array $rows, int $now): array
{
    return array_values(array_filter($rows, function ($r) use ($now) {
        return is_array($r) && (int)($r['t'] ?? 0) > $now - 3600;
    }));
}

function prune_rate(): void
{
    $file = data_dir() . '/rate.php';
    if (!is_file($file)) {
        return;
    }
    [$fh, $rows] = open_locked($file);
    $fresh = rate_rows_fresh($rows, time());
    if (count($fresh) !== count($rows)) {
        write_rows($fh, $fresh);
    }
    close_locked($fh);
}

/** Не больше RATE_LIMIT_PER_HOUR заявок в час с одного адреса. Храним только хеш адреса. */
function rate_ok(string $ip): bool
{
    $key = hash('sha256', $ip . '|' . (string)(cfg()['salt'] ?? 'car-leads'));
    [$fh, $rows] = open_locked(data_dir() . '/rate.php');
    $now = time();
    $fresh = rate_rows_fresh($rows, $now);
    $mine = count(array_filter($fresh, function ($r) use ($key) {
        return ($r['k'] ?? '') === $key;
    }));
    $ok = $mine < RATE_LIMIT_PER_HOUR;
    if ($ok) {
        $fresh[] = ['k' => $key, 't' => $now];
    }
    write_rows($fh, $fresh);
    close_locked($fh);
    return $ok;
}

/**
 * Дописывает заявку и возвращает её номер.
 * Номера выдаёт счётчик в seq.php: он только растёт, удаление строк из leads.php его не уменьшает, поэтому номер
 * не повторяется. epoch меняется только при создании счётчика заново: по нему бот видит, что нумерация начата сначала.
 * Блокировка seq.php держится всё время записи и при чтении заявок ботом: номера появляются в файле строго по порядку.
 */
function append_lead(array $lead): int
{
    $dir = data_dir();
    [$sfh, $srows] = open_locked($dir . '/seq.php');
    [$lfh, $rows] = open_locked($dir . '/leads.php');
    $seq = $srows[0] ?? null;
    if (!is_array($seq) || !isset($seq['epoch'], $seq['last'])) {
        $last = 0;
        foreach ($rows as $r) {
            $last = max($last, (int)($r['id'] ?? 0));
        }
        $seq = ['epoch' => bin2hex(random_bytes(8)), 'last' => $last];
    }
    $id = (int)$seq['last'] + 1;
    $seq['last'] = $id;
    $line = json_encode(['id' => $id] + $lead, JSON_UNESCAPED_UNICODE);
    // Сначала счётчик: если потом не запишется заявка, номер просто пропадёт, но не повторится
    if ($line === false || !write_rows($sfh, [$seq])) {
        json_out(500, ['ok' => false, 'error' => 'storage']);
    }
    fseek($lfh, 0, SEEK_END);
    $size = (int)ftell($lfh);
    $data = ($size === 0 ? DATA_GUARD : '') . $line . "\n";
    if (fwrite($lfh, $data) !== strlen($data) || !fflush($lfh)) {
        ftruncate($lfh, $size);  // не оставляем обрывок строки: к нему приклеилась бы следующая заявка
        json_out(500, ['ok' => false, 'error' => 'storage']);
    }
    close_locked($lfh);
    close_locked($sfh);
    return $id;
}

/** Заявки с номером больше $after (не больше MAX_LEADS_PER_REPLY), номер последней выданной и epoch счётчика.
 *  Заодно удаляет заявки старше KEEP_DAYS дней. */
function leads_after(int $after): array
{
    $dir = data_dir();
    if (!is_file($dir . '/seq.php') && !is_file($dir . '/leads.php')) {
        return [[], 0, ''];
    }
    [$sfh, $srows] = open_locked($dir . '/seq.php');
    [$lfh, $rows] = open_locked($dir . '/leads.php');
    $cutoff = gmdate('Y-m-d\TH:i:s\Z', time() - KEEP_DAYS * 86400);
    $kept = array_values(array_filter($rows, function ($r) use ($cutoff) {
        return (string)($r['ts'] ?? '') >= $cutoff;
    }));
    if (count($kept) !== count($rows)) {
        write_rows($lfh, $kept);
    }
    close_locked($lfh);
    close_locked($sfh);
    $seq = is_array($srows[0] ?? null) ? $srows[0] : [];
    $out = [];
    foreach ($kept as $r) {
        if ((int)($r['id'] ?? 0) > $after && count($out) < MAX_LEADS_PER_REPLY) {
            $out[] = $r;
        }
    }
    usort($out, function ($a, $b) {
        return (int)$a['id'] <=> (int)$b['id'];
    });
    return [$out, (int)($seq['last'] ?? 0), (string)($seq['epoch'] ?? '')];
}
