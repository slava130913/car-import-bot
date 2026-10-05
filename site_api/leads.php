<?php
// Выдача новых заявок боту: GET leads.php?after=<последний полученный номер>, заголовок X-Token.
// Токен пишется в config.php при выкладке; бот считает тот же токен из своего BOT_TOKEN.
// Ответ: leads (новые заявки по возрастанию номера), last (последний выданный номер), epoch (меняется,
// только если счётчик номеров создан заново; тогда бот начинает с нуля и не дублирует уже полученные заявки).
declare(strict_types=1);
require __DIR__ . '/_lib.php';

$token = (string)(cfg()['token'] ?? '');
if ($token === '') {
    json_out(503, ['ok' => false, 'error' => 'not configured']);
}
if (!hash_equals($token, (string)($_SERVER['HTTP_X_TOKEN'] ?? ''))) {
    json_out(403, ['ok' => false, 'error' => 'token']);
}
prune_rate();  // бот опрашивает раз в минуту: хеши адресов не залёживаются дольше часа
$after = max(0, (int)($_GET['after'] ?? 0));
[$leads, $last, $epoch] = leads_after($after);
json_out(200, ['ok' => true, 'leads' => $leads, 'last' => $last, 'epoch' => $epoch]);
