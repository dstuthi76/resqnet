import 'dart:convert';
import 'dart:math';

import 'package:cryptography/cryptography.dart';

/// Packet types that travel through the mesh.
class PacketType {
  static const sos = 'SOS';
  static const ack = 'ACK';
  static const status = 'STATUS';
}

const int maxHops = 8;
const Duration sosTtl = Duration(hours: 6);
const Duration controlTtl = Duration(hours: 1);

String randomId(String prefix) {
  final r = Random.secure();
  final hex = List.generate(8, (_) => r.nextInt(256).toRadixString(16).padLeft(2, '0')).join();
  return '$prefix-${DateTime.now().millisecondsSinceEpoch.toRadixString(36)}-$hex';
}

/// A mesh packet. Stored and relayed as plain JSON maps so unknown fields
/// from newer app versions survive relaying.
class Packet {
  final Map<String, dynamic> data;
  Packet(this.data);

  String get id => data['id'] as String;
  String get type => (data['t'] ?? PacketType.sos) as String;
  int get hops => (data['hops'] ?? 0) as int;
  int get created => (data['created'] ?? DateTime.now().millisecondsSinceEpoch) as int;

  bool get expired {
    final age = DateTime.now().millisecondsSinceEpoch - created;
    final ttl = type == PacketType.sos ? sosTtl : controlTtl;
    return age > ttl.inMilliseconds;
  }

  Packet relayed() => Packet({...data, 'hops': hops + 1});

  Map<String, dynamic> toJson() => data;

  /// Fields sent to the cloud for an SOS (drops mesh-only fields).
  Map<String, dynamic> toUpload() {
    final m = Map<String, dynamic>.from(data);
    m.remove('t');
    m.remove('created');
    m['channel'] = 'mesh';
    return m;
  }
}

/// Device identity: an Ed25519 key pair so relays cannot alter an SOS.
class Identity {
  final String deviceId;
  final SimpleKeyPair keyPair;
  final String publicKeyB64;
  Identity(this.deviceId, this.keyPair, this.publicKeyB64);

  static final _algo = Ed25519();

  static Future<Identity> load(String? deviceId, String? seedB64,
      Future<void> Function(String id, String seed) save) async {
    SimpleKeyPair kp;
    String id = deviceId ?? randomId('dev');
    if (seedB64 != null) {
      kp = await _algo.newKeyPairFromSeed(base64Decode(seedB64));
    } else {
      kp = await _algo.newKeyPair();
      final seed = await kp.extractPrivateKeyBytes();
      await save(id, base64Encode(seed));
    }
    final pub = await kp.extractPublicKey();
    return Identity(id, kp, base64Encode(pub.bytes));
  }

  /// Must match backend/security.py canonical():
  /// id|lat|lng|ts|people|needs|text|device_id
  static String canonical(Map<String, dynamic> p) {
    String f(dynamic v) => v == null ? '' : (v as num).toDouble().toStringAsFixed(6);
    final needs = List<String>.from(p['needs'] ?? const [])..sort();
    final people = p['people'];
    return [
      p['id'] ?? '',
      f(p['lat']),
      f(p['lng']),
      p['ts'] ?? '',
      people == null ? '' : '$people',
      needs.join(','),
      p['text'] ?? '',
      p['device_id'] ?? '',
    ].join('|');
  }

  Future<Packet> newSos({
    required String text,
    required int people,
    required List<String> needs,
    double? lat,
    double? lng,
  }) async {
    final p = <String, dynamic>{
      't': PacketType.sos,
      'id': randomId('sos'),
      'text': text,
      'people': people,
      'needs': needs,
      'lat': lat == null ? null : double.parse(lat.toStringAsFixed(6)),
      'lng': lng == null ? null : double.parse(lng.toStringAsFixed(6)),
      'ts': DateTime.now().toUtc().toIso8601String(),
      'device_id': deviceId,
      'hops': 0,
      'created': DateTime.now().millisecondsSinceEpoch,
    };
    final sig = await _algo.sign(utf8.encode(canonical(p)), keyPair: keyPair);
    p['sig'] = base64Encode(sig.bytes);
    p['pk'] = publicKeyB64;
    return Packet(p);
  }
}
