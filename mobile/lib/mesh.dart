import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:nearby_connections/nearby_connections.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'packet.dart';

const String serviceId = 'org.resqnet.mesh';
const String defaultServer = String.fromEnvironment('SERVER_URL', defaultValue: 'http://192.168.1.10:8000');

class SosStatus {
  final String text;
  final int version;
  SosStatus(this.text, this.version);
}

/// Store-and-forward mesh:
///  * every phone keeps the packets it hears and passes them to every peer,
///  * duplicates are skipped by message id, hop count and expiry stop loops,
///  * the first phone with internet uploads everything; the cloud's ACK spreads
///    back so phones delete delivered messages,
///  * rescue status ("help on the way") travels back the same way.
class MeshService extends ChangeNotifier {
  final Nearby _nearby = Nearby();
  late SharedPreferences _prefs;
  late Identity identity;

  final Map<String, Packet> store = {};          // packets waiting to be relayed / uploaded
  final Set<String> seen = {};                   // SOS ids already handled
  final Set<String> acked = {};                  // SOS ids the cloud confirmed
  final Map<String, SosStatus> statuses = {};    // SOS id -> latest rescue status
  final List<String> mine = [];                  // ids of SOS sent from this phone
  final Map<String, String> peers = {};          // endpointId -> name
  final List<String> logs = [];
  List<Map<String, dynamic>> shelters = [];

  String server = defaultServer;
  bool relayEnabled = true;
  bool running = false;
  DateTime? lastOnline;
  Timer? _timer;

  bool get online => lastOnline != null && DateTime.now().difference(lastOnline!) < const Duration(seconds: 40);
  String get myName => 'RQ-${identity.deviceId}';

  Future<void> init() async {
    _prefs = await SharedPreferences.getInstance();
    identity = await Identity.load(
      _prefs.getString('device_id'),
      _prefs.getString('key_seed'),
      (id, seed) async {
        await _prefs.setString('device_id', id);
        await _prefs.setString('key_seed', seed);
      },
    );
    server = _prefs.getString('server') ?? defaultServer;
    relayEnabled = _prefs.getBool('relay') ?? true;
    _restore();
    notifyListeners();
  }

  // ------------------------------------------------------------ persistence
  void _restore() {
    try {
      final raw = _prefs.getString('state');
      if (raw == null) return;
      final s = jsonDecode(raw) as Map<String, dynamic>;
      for (final p in (s['store'] as List? ?? [])) {
        final pk = Packet(Map<String, dynamic>.from(p as Map));
        if (!pk.expired) store[pk.id] = pk;
      }
      seen.addAll(List<String>.from(s['seen'] ?? []));
      acked.addAll(List<String>.from(s['acked'] ?? []));
      mine.addAll(List<String>.from(s['mine'] ?? []));
      (s['statuses'] as Map? ?? {}).forEach((k, v) => statuses[k as String] = SosStatus(v['text'], v['v']));
      shelters = List<Map<String, dynamic>>.from((s['shelters'] as List? ?? []).map((e) => Map<String, dynamic>.from(e)));
    } catch (e) {
      log('could not restore state: $e');
    }
  }

  Future<void> _save() async {
    final s = {
      'store': store.values.map((p) => p.toJson()).toList(),
      'seen': seen.take(2000).toList(),
      'acked': acked.take(2000).toList(),
      'mine': mine,
      'statuses': statuses.map((k, v) => MapEntry(k, {'text': v.text, 'v': v.version})),
      'shelters': shelters,
    };
    await _prefs.setString('state', jsonEncode(s));
  }

  Future<void> setServer(String url) async {
    server = url.trim().replaceAll(RegExp(r'/+$'), '');
    await _prefs.setString('server', server);
    notifyListeners();
  }

  Future<void> setRelay(bool on) async {
    relayEnabled = on;
    await _prefs.setBool('relay', on);
    if (on) {
      await start();
    } else {
      await stop();
    }
    notifyListeners();
  }

  void log(String s) {
    final t = DateTime.now();
    logs.insert(0, '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}:${t.second.toString().padLeft(2, '0')} $s');
    if (logs.length > 100) logs.removeLast();
    notifyListeners();
  }

  // --------------------------------------------------------------- lifecycle
  Future<void> start() async {
    if (running) return;
    running = true;
    if (relayEnabled) {
      await _startNearby();
    }
    _timer = Timer.periodic(const Duration(seconds: 10), (_) => _tick());
    _tick();
  }

  Future<void> stop() async {
    try {
      await _nearby.stopAdvertising();
      await _nearby.stopDiscovery();
      await _nearby.stopAllEndpoints();
    } catch (_) {}
    peers.clear();
    _timer?.cancel();
    running = false;
    notifyListeners();
  }

  Future<void> _startNearby() async {
    try {
      await _nearby.startAdvertising(
        myName,
        Strategy.P2P_CLUSTER,
        onConnectionInitiated: _onConnectionInitiated,
        onConnectionResult: _onConnectionResult,
        onDisconnected: _onDisconnected,
        serviceId: serviceId,
      );
      log('advertising as $myName');
    } catch (e) {
      log('advertising failed: $e');
    }
    try {
      await _nearby.startDiscovery(
        myName,
        Strategy.P2P_CLUSTER,
        onEndpointFound: (id, name, sid) => _onFound(id, name),
        onEndpointLost: (id) {},
        serviceId: serviceId,
      );
      log('discovering nearby ResQNet phones');
    } catch (e) {
      log('discovery failed: $e');
    }
  }

  void _onFound(String id, String name) {
    if (!name.startsWith('RQ-') || peers.containsKey(id)) return;
    // Tie-break so both phones don't request at once; the other side retries after a delay.
    final delay = myName.compareTo(name) < 0 ? Duration.zero : const Duration(seconds: 6);
    Future.delayed(delay, () async {
      if (peers.containsKey(id)) return;
      try {
        await _nearby.requestConnection(
          myName,
          id,
          onConnectionInitiated: _onConnectionInitiated,
          onConnectionResult: _onConnectionResult,
          onDisconnected: _onDisconnected,
        );
      } catch (e) {
        debugPrint('requestConnection $id: $e');
      }
    });
  }

  void _onConnectionInitiated(String id, ConnectionInfo info) {
    _nearby.acceptConnection(
      id,
      onPayLoadRecieved: (endpointId, payload) {
        if (payload.type == PayloadType.BYTES && payload.bytes != null) {
          _onBytes(endpointId, payload.bytes!);
        }
      },
      onPayloadTransferUpdate: (endpointId, update) {},
    );
    peers[id] = info.endpointName;
  }

  void _onConnectionResult(String id, Status status) {
    if (status == Status.CONNECTED) {
      log('connected to ${peers[id] ?? id}');
      _sendTo(id, store.values.toList());
    } else {
      peers.remove(id);
    }
    notifyListeners();
  }

  void _onDisconnected(String id) {
    log('peer ${peers[id] ?? id} left');
    peers.remove(id);
    notifyListeners();
  }

  // ------------------------------------------------------------- relaying
  void _sendTo(String endpointId, List<Packet> packets) {
    final list = packets.where((p) => !p.expired).toList();
    for (var i = 0; i < list.length; i += 15) {
      final chunk = list.sublist(i, i + 15 > list.length ? list.length : i + 15);
      final bytes = Uint8List.fromList(utf8.encode(jsonEncode({'packets': chunk.map((p) => p.toJson()).toList()})));
      _nearby.sendBytesPayload(endpointId, bytes).catchError((e) => debugPrint('send failed: $e'));
    }
  }

  void _broadcast(List<Packet> packets, {String? except}) {
    if (packets.isEmpty) return;
    for (final id in peers.keys.toList()) {
      if (id != except) _sendTo(id, packets);
    }
  }

  void _onBytes(String from, Uint8List bytes) {
    try {
      final msg = jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
      final fresh = <Packet>[];
      for (final raw in (msg['packets'] as List? ?? [])) {
        final p = Packet(Map<String, dynamic>.from(raw as Map));
        final r = _accept(p);
        if (r != null) fresh.add(r);
      }
      if (fresh.isNotEmpty) {
        _broadcast(fresh, except: from);
        _save();
        notifyListeners();
      }
    } catch (e) {
      debugPrint('bad payload from $from: $e');
    }
  }

  /// Returns the packet to forward, or null if it should be dropped.
  Packet? _accept(Packet p) {
    if (p.expired || p.hops >= maxHops) return null;
    switch (p.type) {
      case PacketType.ack:
        final ids = List<String>.from(p.data['ids'] ?? []);
        final newIds = ids.where((i) => !acked.contains(i)).toList();
        if (newIds.isEmpty) return null;
        acked.addAll(newIds);
        for (final i in newIds) {
          if (store.remove(i) != null) log('SOS ${_short(i)} delivered - removed from phone');
        }
        final out = p.relayed();
        store[out.id] = out;
        return out;
      case PacketType.status:
        final ref = p.data['ref'] as String;
        final v = (p.data['v'] ?? 0) as int;
        final prev = statuses[ref];
        if (prev != null && prev.version >= v) return null;
        statuses[ref] = SosStatus(p.data['text'] as String, v);
        if (mine.contains(ref)) log('🚤 ${p.data['text']}');
        final out = p.relayed();
        store[out.id] = out;
        return out;
      default:
        if (seen.contains(p.id) || acked.contains(p.id)) return null;
        seen.add(p.id);
        final out = p.relayed();
        store[out.id] = out;
        log('relaying SOS ${_short(p.id)} (hop ${out.hops})');
        return out;
    }
  }

  String _short(String id) => id.length > 4 ? id.substring(id.length - 4) : id;

  // ----------------------------------------------------------------- SOS
  Future<void> sendSos(Packet p) async {
    store[p.id] = p;
    seen.add(p.id);
    mine.add(p.id);
    log('SOS created ${_short(p.id)}');
    _broadcast([p]);
    await _save();
    notifyListeners();
    _tick();
  }

  // -------------------------------------------------------- cloud sync
  bool _syncing = false;

  Future<void> _tick() async {
    store.removeWhere((_, p) => p.expired);
    if (_syncing) return;
    _syncing = true;
    try {
      await _upload();
      await _pollStatus();
      await _fetchShelters();
    } finally {
      _syncing = false;
      notifyListeners();
    }
  }

  Future<void> _upload() async {
    final sos = store.values.where((p) => p.type == PacketType.sos).toList();
    try {
      if (sos.isEmpty) {
        final r = await http.get(Uri.parse('$server/api/health')).timeout(const Duration(seconds: 6));
        if (r.statusCode == 200) lastOnline = DateTime.now();
        return;
      }
      final r = await http
          .post(Uri.parse('$server/api/sos'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode(sos.map((p) => p.toUpload()).toList()))
          .timeout(const Duration(seconds: 15));
      if (r.statusCode != 200) return;
      lastOnline = DateTime.now();
      final ids = List<String>.from(jsonDecode(r.body)['acks'] ?? []);
      if (ids.isEmpty) return;
      for (final i in ids) {
        store.remove(i);
      }
      acked.addAll(ids);
      final ack = Packet({'t': PacketType.ack, 'id': randomId('ack'), 'ids': ids, 'hops': 0,
        'created': DateTime.now().millisecondsSinceEpoch});
      store[ack.id] = ack;
      log('⬆ uploaded ${ids.length} SOS to the control room');
      _broadcast([ack]);
      await _save();
    } catch (_) {
      // offline: keep everything for the mesh
    }
  }

  Future<void> _pollStatus() async {
    if (!online) return;
    final watch = {...mine, ...seen}.where((i) => i.startsWith('sos-')).take(100).toList();
    if (watch.isEmpty) return;
    try {
      final r = await http.get(Uri.parse('$server/api/sos/status?ids=${watch.join(',')}')).timeout(const Duration(seconds: 10));
      if (r.statusCode != 200) return;
      final m = jsonDecode(r.body) as Map<String, dynamic>;
      final out = <Packet>[];
      m.forEach((ref, s) {
        final st = s['status'] as String? ?? 'received';
        final v = const {'received': 1, 'reviewing': 1, 'open': 2, 'claimed': 3, 'closed': 4}[st] ?? 1;
        final text = st == 'claimed'
            ? 'Help on the way: ${s['team']}, about ${s['eta_min']} min'
            : st == 'closed'
                ? 'Rescue team marked you as reached'
                : 'Received by the control room';
        final prev = statuses[ref];
        if (prev == null || prev.version < v) {
          statuses[ref] = SosStatus(text, v);
          final p = Packet({'t': PacketType.status, 'id': 'st-$ref-$v', 'ref': ref, 'text': text, 'v': v,
            'hops': 0, 'created': DateTime.now().millisecondsSinceEpoch});
          store[p.id] = p;
          out.add(p);
        }
      });
      if (out.isNotEmpty) {
        _broadcast(out);
        await _save();
      }
    } catch (_) {}
  }

  Future<void> _fetchShelters() async {
    if (!online || shelters.isNotEmpty) return;
    try {
      final r = await http.get(Uri.parse('$server/api/shelters')).timeout(const Duration(seconds: 10));
      if (r.statusCode == 200) {
        shelters = List<Map<String, dynamic>>.from((jsonDecode(r.body) as List).map((e) => Map<String, dynamic>.from(e)));
        await _save();
        log('saved ${shelters.length} shelters for offline use');
      }
    } catch (_) {}
  }
}
