#!/usr/bin/env python3
"""Genera la coppia di chiavi VAPID per il Web Push. Da lanciare una volta sola.

Le chiavi identificano il server verso i push service di Apple/Google. Se le
rigeneri, tutte le subscription esistenti smettono di funzionare e i dispositivi
vanno reiscritti.
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


key = ec.generate_private_key(ec.SECP256R1())
private_raw = key.private_numbers().private_value.to_bytes(32, "big")
public_raw = key.public_key().public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint,
)

print("# Incolla queste righe in powerwatch.env")
print(f"PW_VAPID_PRIVATE_KEY={b64(private_raw)}")
print(f"PW_VAPID_PUBLIC_KEY={b64(public_raw)}")
print("PW_VAPID_SUBJECT=mailto:tua.mail@example.com")
