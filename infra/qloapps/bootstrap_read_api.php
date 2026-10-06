<?php

declare(strict_types=1);

require '/home/qloapps/www/QloApps/config/config.inc.php';

if (!Configuration::updateValue('PS_WEBSERVICE', 1)) {
    fwrite(STDERR, "Unable to enable the QloApps webservice.\n");
    exit(1);
}

$key = getenv('QLOAPPS_API_KEY');
if (!is_string($key) || !preg_match('/^[A-Za-z0-9]{32}$/', $key)) {
    fwrite(STDERR, "QLOAPPS_API_KEY must contain exactly 32 alphanumeric characters.\n");
    exit(1);
}

$resources = [
    'addresses',
    'booking_extra_demands',
    'bookings',
    'customers',
    'customer_messages',
    'customer_threads',
    'extra_demands',
    'hotel_ari',
    'hotel_rooms',
    'hotels',
    'order_details',
    'order_invoices',
    'orders',
    'room_bookings',
    'room_types',
    'services',
    'tax_rules',
    'taxes',
];

$existingResources = array_keys(WebserviceRequest::getResources());
$missingResources = array_values(array_diff($resources, $existingResources));
if ($missingResources) {
    fwrite(STDERR, 'Missing QloApps API resources: '.implode(', ', $missingResources)."\n");
    exit(1);
}

$accountId = (int) Db::getInstance()->getValue(
    'SELECT `id_webservice_account` FROM `'._DB_PREFIX_."webservice_account` WHERE `key` = '".pSQL($key)."'"
);
$created = false;

if (!$accountId) {
    $account = new WebserviceKey();
    $account->key = $key;
    $account->active = true;
    $account->description = 'AgentPMS read-only adapter';
    if (!$account->add()) {
        fwrite(STDERR, "Unable to create the QloApps API account.\n");
        exit(1);
    }
    $accountId = (int) $account->id;
    $created = true;
}

$permissions = [];
foreach ($resources as $resource) {
    $permissions[$resource] = ['GET' => true, 'HEAD' => true];
}
$permissions['hotel_ari']['POST'] = true;

if (!WebserviceKey::setPermissionForAccount($accountId, $permissions)) {
    fwrite(STDERR, "Unable to configure the QloApps API permissions.\n");
    exit(1);
}

echo json_encode([
    'account_id' => $accountId,
    'created' => $created,
    'webservice_enabled' => (bool) Configuration::get('PS_WEBSERVICE'),
    'methods' => [
        'default' => ['GET', 'HEAD'],
        'hotel_ari' => ['GET', 'HEAD', 'POST'],
    ],
    'resources' => $resources,
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES).PHP_EOL;
