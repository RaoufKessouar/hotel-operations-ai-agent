<?php

declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');

function respond(int $status, array $payload): void
{
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

$adminDirectories = array_values(array_filter(
    glob(__DIR__.'/admin*', GLOB_ONLYDIR) ?: [],
    static fn (string $path): bool => is_file($path.'/functions.php')
        && is_dir($path.'/themes')
));
if (count($adminDirectories) !== 1) {
    respond(500, ['error' => 'admin_context_unavailable']);
}
if (!defined('_PS_ADMIN_DIR_')) {
    define('_PS_ADMIN_DIR_', $adminDirectories[0]);
}
if (!defined('PS_ADMIN_DIR')) {
    define('PS_ADMIN_DIR', _PS_ADMIN_DIR_);
}
require __DIR__.'/config/config.inc.php';
require_once _PS_ADMIN_DIR_.'/functions.php';

$expectedKey = getenv('QLOAPPS_WRITE_API_KEY');
$authorization = $_SERVER['HTTP_AUTHORIZATION']
    ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION']
    ?? '';
$providedKey = (string) ($_SERVER['PHP_AUTH_USER'] ?? '');
if (preg_match('/^Basic\s+(.+)$/i', $authorization, $matches)) {
    $decoded = base64_decode($matches[1], true);
    if (is_string($decoded)) {
        $providedKey = explode(':', $decoded, 2)[0];
    }
}
$isAuthorized = is_string($expectedKey)
    && preg_match('/^[A-Za-z0-9]{32}$/', $expectedKey)
    && hash_equals($expectedKey, $providedKey);
if (!$isAuthorized && preg_match('/^[A-Za-z0-9]{32}$/', $providedKey)) {
    $writeAccountId = (int) Db::getInstance()->getValue(
        'SELECT `id_webservice_account` FROM `'._DB_PREFIX_.'webservice_account`'
        ." WHERE `key` = '".pSQL($providedKey)."'"
        ." AND `active` = 1"
        ." AND `description` = 'AgentPMS confirmed booking writes'"
    );
    $isAuthorized = $writeAccountId > 0;
}
if (
    !$isAuthorized
) {
    header('WWW-Authenticate: Basic realm="AgentPMS QloApps bridge"');
    respond(401, ['error' => 'unauthorized']);
}
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    header('Allow: POST');
    respond(405, ['error' => 'method_not_allowed']);
}

$rawBody = file_get_contents('php://input');
$payload = json_decode(is_string($rawBody) ? $rawBody : '', true);
if (!is_array($payload)) {
    respond(400, ['error' => 'invalid_action']);
}
$action = (string) ($payload['action'] ?? '');
if (!in_array($action, ['issue_invoice', 'update_booking'], true)) {
    respond(400, ['error' => 'invalid_action']);
}

if ($action === 'update_booking') {
    $reservationId = filter_var($payload['reservation_id'] ?? null, FILTER_VALIDATE_INT);
    $adults = filter_var($payload['adults'] ?? null, FILTER_VALIDATE_INT);
    $children = filter_var($payload['children'] ?? null, FILTER_VALIDATE_INT);
    $arrivalDate = (string) ($payload['arrival_date'] ?? '');
    $departureDate = (string) ($payload['departure_date'] ?? '');
    $parsedArrival = DateTimeImmutable::createFromFormat('!Y-m-d', $arrivalDate);
    $parsedDeparture = DateTimeImmutable::createFromFormat('!Y-m-d', $departureDate);
    if (!$reservationId || $reservationId <= 0) {
        respond(422, ['error' => 'invalid_reservation_id']);
    }
    if (!$parsedArrival || $parsedArrival->format('Y-m-d') !== $arrivalDate
        || !$parsedDeparture || $parsedDeparture->format('Y-m-d') !== $departureDate
        || $parsedDeparture <= $parsedArrival
    ) {
        respond(422, ['error' => 'invalid_date_range']);
    }
    if ($adults === false || $adults <= 0 || $children === false || $children < 0) {
        respond(422, ['error' => 'invalid_occupancy']);
    }

    try {
        $rows = Db::getInstance()->executeS(
            'SELECT `id` FROM `'._DB_PREFIX_.'htl_booking_detail`'
            .' WHERE `id_order` = '.(int) $reservationId
            .' AND `is_cancelled` = 0 AND `is_refunded` = 0'
        );
        if (!is_array($rows) || count($rows) !== 1) {
            respond(409, ['error' => 'complex_booking_update_unsupported']);
        }
        $bookingDetail = new HotelBookingDetail((int) $rows[0]['id']);
        $order = new Order((int) $reservationId);
        $orderDetail = new OrderDetail((int) $bookingDetail->id_order_detail);
        $cart = new Cart((int) $bookingDetail->id_cart);
        if (!Validate::isLoadedObject($bookingDetail)
            || !Validate::isLoadedObject($order)
            || !Validate::isLoadedObject($orderDetail)
            || !Validate::isLoadedObject($cart)
        ) {
            respond(404, ['error' => 'booking_not_found']);
        }

        $currentArrival = date('Y-m-d', strtotime((string) $bookingDetail->date_from));
        $currentDeparture = date('Y-m-d', strtotime((string) $bookingDetail->date_to));
        if ($currentArrival === $arrivalDate
            && $currentDeparture === $departureDate
            && (int) $bookingDetail->adults === $adults
            && (int) $bookingDetail->children === $children
        ) {
            respond(200, ['result' => true, 'already_applied' => true]);
        }

        $childAges = json_decode((string) $bookingDetail->child_ages, true);
        if (!is_array($childAges)) {
            $childAges = [];
        }
        if ($children > 0 && ($children !== (int) $bookingDetail->children
            || count($childAges) !== $children)
        ) {
            respond(422, ['error' => 'children_ages_required']);
        }

        $oldNights = (int) HotelHelper::getNumberOfDays(
            (string) $bookingDetail->date_from,
            (string) $bookingDetail->date_to
        );
        if ($oldNights <= 0) {
            respond(409, ['error' => 'invalid_existing_stay']);
        }
        $roomUnitPriceExcl = (float) $bookingDetail->total_price_tax_excl / $oldNights;
        $roomUnitPriceIncl = (float) $bookingDetail->total_price_tax_incl / $oldNights;

        $employee = new Employee(1);
        if (!Validate::isLoadedObject($employee) || !$employee->isSuperAdmin()) {
            foreach (Employee::getEmployees(true) as $candidate) {
                $candidateEmployee = new Employee((int) $candidate['id_employee']);
                if (Validate::isLoadedObject($candidateEmployee)
                    && $candidateEmployee->isSuperAdmin()
                ) {
                    $employee = $candidateEmployee;
                    break;
                }
            }
        }
        if (!Validate::isLoadedObject($employee) || !$employee->isSuperAdmin()) {
            respond(500, ['error' => 'superadmin_context_unavailable']);
        }

        $context = Context::getContext();
        $context->employee = $employee;
        $context->cookie->id_employee = (int) $employee->id;
        Cache::store('isLoggedBack'.(int) $employee->id, true);
        $context->cart = $cart;
        $context->currency = new Currency((int) $order->id_currency);
        $context->shop = new Shop((int) $order->id_shop);

        $request = [
            'id_order' => (int) $order->id,
            'id_order_detail' => (int) $orderDetail->id,
            'id_booking_detail' => (int) $bookingDetail->id,
            'edit_product' => [
                'date_from' => $arrivalDate,
                'date_to' => $departureDate,
            ],
            'date_from' => (string) $bookingDetail->date_from,
            'date_to' => (string) $bookingDetail->date_to,
            'id_hotel' => (int) $bookingDetail->id_hotel,
            'id_room' => (int) $bookingDetail->id_room,
            'id_product' => (int) $bookingDetail->id_product,
            'room_unit_price' => (string) $roomUnitPriceExcl,
            'product_price_tax_excl' => (string) $roomUnitPriceExcl,
            'product_price_tax_incl' => (string) $roomUnitPriceIncl,
            'occupancy' => [[
                'adults' => $adults,
                'children' => $children,
                'child_ages' => $childAges,
            ]],
        ];
        if ($order->hasInvoice()) {
            $invoices = $order->getInvoicesCollection()->getResults();
            if ($invoices) {
                $request['product_invoice'] = (int) $invoices[0]->id;
            }
        }
        $_POST = array_merge($_POST, $request);
        $_REQUEST = array_merge($_REQUEST, $request);

        $controller = new AdminOrdersController();
        $controller->tabAccess = ['view' => 1, 'edit' => 1];
        $controller->ajaxProcessEditRoomOnOrder();
        respond(500, ['error' => 'booking_update_no_response']);
    } catch (Throwable $error) {
        PrestaShopLogger::addLog(
            'AgentPMS booking update bridge failed for order '.(int) $reservationId
            .': '.$error->getMessage().' at '.basename($error->getFile())
            .':'.$error->getLine(),
            3,
            null,
            'Order',
            (int) $reservationId,
            true
        );
        respond(500, ['error' => 'booking_update_failed']);
    }
}

$orderId = filter_var($payload['order_id'] ?? null, FILTER_VALIDATE_INT);
if (!$orderId || $orderId <= 0) {
    respond(422, ['error' => 'invalid_order_id']);
}

try {
    $order = new Order((int) $orderId);
    if (!Validate::isLoadedObject($order)) {
        respond(404, ['error' => 'order_not_found']);
    }
    if (!$order->hasInvoice()) {
        $order->setInvoice(true);
        $order = new Order((int) $orderId);
    }
    $invoices = [];
    foreach ($order->getInvoicesCollection()->getResults() as $invoice) {
        $invoices[] = [
            'id' => (int) $invoice->id,
            'id_order' => (int) $invoice->id_order,
            'number' => (int) $invoice->number,
            'total_paid_tax_excl' => (string) $invoice->total_paid_tax_excl,
            'total_paid_tax_incl' => (string) $invoice->total_paid_tax_incl,
            'date_add' => (string) $invoice->date_add,
        ];
    }
    if (!$invoices) {
        respond(500, ['error' => 'invoice_not_created']);
    }
    PrestaShopLogger::addLog(
        'AgentPMS issued or retrieved invoice for order '.(int) $orderId,
        1,
        null,
        'Order',
        (int) $orderId,
        true
    );
    respond(200, ['invoices' => $invoices]);
} catch (Throwable $error) {
    PrestaShopLogger::addLog(
        'AgentPMS invoice bridge failed for order '.(int) $orderId,
        3,
        null,
        'Order',
        (int) $orderId,
        true
    );
    respond(500, ['error' => 'invoice_operation_failed']);
}
