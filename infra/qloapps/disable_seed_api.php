<?php

declare(strict_types=1);

require '/home/qloapps/www/QloApps/config/config.inc.php';

$key = getenv('QLOAPPS_SEED_API_KEY');
if (!is_string($key) || !preg_match('/^[A-Za-z0-9]{32}$/', $key)) {
    fwrite(STDERR, "QLOAPPS_SEED_API_KEY must contain exactly 32 alphanumeric characters.\n");
    exit(1);
}

$accountId = (int) Db::getInstance()->getValue(
    'SELECT `id_webservice_account` FROM `'._DB_PREFIX_."webservice_account` WHERE `key` = '".pSQL($key)."'"
);
if (!$accountId) {
    echo json_encode(['active' => false, 'found' => false]).PHP_EOL;
    exit(0);
}

$account = new WebserviceKey($accountId);
$account->active = false;
if (!$account->update()) {
    fwrite(STDERR, "Unable to disable the QloApps seed API account.\n");
    exit(1);
}

echo json_encode([
    'account_id' => $accountId,
    'active' => false,
    'found' => true,
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES).PHP_EOL;
