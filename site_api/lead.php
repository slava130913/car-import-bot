<?php
// Приём заявки с формы на сайте. POST JSON: name, phone, city, when, model, calc, total, consent, website (ловушка), elapsed.
declare(strict_types=1);
require __DIR__ . '/_lib.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    json_out(405, ['ok' => false, 'error' => 'method']);
}
$in = json_decode((string)file_get_contents('php://input', false, null, 0, 16384), true);
if (!is_array($in)) {
    $in = $_POST;
}

// Ловушка для ботов: поле скрыто от людей. Отвечаем «принято», чтобы бот не подбирал обход.
if (clean($in['website'] ?? '', 200) !== '' || (int)($in['elapsed'] ?? 0) < 3000) {
    json_out(200, ['ok' => true, 'id' => 0]);
}

$phone = clean($in['phone'] ?? '', 40);
$digits = preg_replace('/\D+/', '', $phone);
if (strlen((string)$digits) < 10 || strlen((string)$digits) > 15) {
    json_out(422, ['ok' => false, 'error' => 'phone']);
}
if (($in['consent'] ?? false) !== true && ($in['consent'] ?? '') !== '1') {
    json_out(422, ['ok' => false, 'error' => 'consent']);
}
if (!rate_ok((string)($_SERVER['REMOTE_ADDR'] ?? ''))) {
    json_out(429, ['ok' => false, 'error' => 'rate']);
}

$when = clean($in['when'] ?? '', 10);
$total = (int)($in['total'] ?? 0);
$id = append_lead([
    'ts' => gmdate('Y-m-d\TH:i:s\Z'),
    'name' => clean($in['name'] ?? '', 80),
    'phone' => $phone,
    'city' => clean($in['city'] ?? '', 60),
    'when' => in_array($when, ['now', 'soon', 'later'], true) ? $when : '',
    'model' => clean($in['model'] ?? '', 80),
    'calc' => clean($in['calc'] ?? '', 400),
    'total' => $total > 0 && $total < 1000000000 ? $total : 0,
    'consent' => true,
]);
json_out(200, ['ok' => true, 'id' => $id]);
