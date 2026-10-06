<?php

declare(strict_types=1);

require '/home/qloapps/www/QloApps/config/config.inc.php';

$catalogPrices = [
    // Prix HT; la TVA standard QloApps produit les tarifs TTC suivants :
    // General 120 EUR, Delux 180 EUR, Executive 240 EUR, Luxury 300 EUR.
    1 => 100.00,
    2 => 150.00,
    3 => 200.00,
    4 => 250.00,
    // Frais obligatoires de l'image de demonstration inclus auparavant dans
    // chaque nuit. Le tarif de chambre doit deja representer le prix complet.
    5 => 0.00,
    // Petit-dejeuner : 12,50 EUR HT, soit 15 EUR TTC par personne et par nuit.
    9 => 12.50,
];

foreach ($catalogPrices as $productId => $price) {
    $product = new Product((int) $productId);
    if (!Validate::isLoadedObject($product)) {
        fwrite(STDERR, "QloApps demo product {$productId} not found.\n");
        exit(1);
    }
    $product->price = (float) $price;
    if (!$product->update()) {
        fwrite(STDERR, "Unable to update demo product {$productId}.\n");
        exit(1);
    }
    Db::getInstance()->update(
        'product_shop',
        ['price' => (float) $price],
        '`id_product` = '.(int) $productId
    );
}

$breakfastId = 9;
$breakfast = new Product($breakfastId);
if (!Validate::isLoadedObject($breakfast)) {
    fwrite(STDERR, "QloApps demo breakfast product not found.\n");
    exit(1);
}

// Le petit-dejeuner de demonstration est vendu par personne et par nuit.
// Le catalogue QloApps d'origine interdit plusieurs unites; l'API reduit
// alors silencieusement une quantite 2 a 1. Le laboratoire doit au contraire
// conserver la quantite confirmee par la reception.
$breakfast->allow_multiple_quantity = true;
if (!$breakfast->update()) {
    fwrite(STDERR, "Unable to configure the demo breakfast product.\n");
    exit(1);
}

Db::getInstance()->update(
    'product_shop',
    ['allow_multiple_quantity' => 1],
    '`id_product` = '.(int) $breakfastId
);

echo json_encode([
    'room_prices_excluding_tax' => [
        'General Rooms' => 100.00,
        'Delux Rooms' => 150.00,
        'Executive Rooms' => 200.00,
        'Luxury Rooms' => 250.00,
    ],
    'mandatory_room_fee_excluding_tax' => 0.00,
    'breakfast_product_id' => $breakfastId,
    'breakfast_price_excluding_tax' => 12.50,
    'allow_multiple_quantity' => true,
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES).PHP_EOL;
