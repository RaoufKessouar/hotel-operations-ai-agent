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
$created = false;

if ($accountId) {
    $account = new WebserviceKey($accountId);
    $account->active = true;
    $account->description = 'AgentPMS synthetic fixture seed (temporary)';
    if (!$account->update()) {
        fwrite(STDERR, "Unable to activate the QloApps seed API account.\n");
        exit(1);
    }
} else {
    $account = new WebserviceKey();
    $account->key = $key;
    $account->active = true;
    $account->description = 'AgentPMS synthetic fixture seed (temporary)';
    if (!$account->add()) {
        fwrite(STDERR, "Unable to create the QloApps seed API account.\n");
        exit(1);
    }
    $accountId = (int) $account->id;
    $created = true;
}

$permissions = [
    'bookings' => ['GET' => true, 'HEAD' => true, 'POST' => true],
    'customer_threads' => ['GET' => true, 'HEAD' => true, 'POST' => true],
    'customer_messages' => ['GET' => true, 'HEAD' => true, 'POST' => true],
];
if (!WebserviceKey::setPermissionForAccount($accountId, $permissions)) {
    fwrite(STDERR, "Unable to configure the QloApps seed API permissions.\n");
    exit(1);
}

echo json_encode([
    'account_id' => $accountId,
    'active' => true,
    'created' => $created,
    'permissions' => $permissions,
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES).PHP_EOL;
