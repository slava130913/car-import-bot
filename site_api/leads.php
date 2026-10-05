<?php
// Выдача новых заявок боту: GET leads.php?after=<последний полученный номер>, заголовок X-Token.
// Токен пишется в config.php при выкладке; бот считает тот же токен из своего BOT_TOKEN.
declare(strict_types=1);
require __DIR__ . '/_lib.php';

$token = (string)(cfg()['token'] ?? '');
if ($token === '') {
    json_out(503, ['ok' => false, 'error' => 'not configured']);
}
if (!hash_equals($token, (string)($_SERVER['HTTP_X_TOKEN'] ?? ''))) {
    json_out(403, ['ok' => false, 'error' => 'token']);
}
$after = max(0, (int)($_GET['after'] ?? 0));
[$leads, $last] = leads_after($after);
json_out(200, ['ok' => true, 'leads' => $leads, 'last' => $last]);
