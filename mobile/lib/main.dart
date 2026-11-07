import 'dart:math';

import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:permission_handler/permission_handler.dart';

import 'mesh.dart';

final mesh = MeshService();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await mesh.init();
  runApp(const ResQNetApp());
}

class ResQNetApp extends StatelessWidget {
  const ResQNetApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ResQNet',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: const Color(0xFFD32F2F), useMaterial3: true),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  String? permissionProblem;

  @override
  void initState() {
    super.initState();
    mesh.addListener(_refresh);
    _boot();
  }

  @override
  void dispose() {
    mesh.removeListener(_refresh);
    super.dispose();
  }

  void _refresh() {
    if (mounted) setState(() {});
  }

  Future<void> _boot() async {
    final statuses = await [
      Permission.location,
      Permission.bluetooth,
      Permission.bluetoothScan,
      Permission.bluetoothAdvertise,
      Permission.bluetoothConnect,
      Permission.nearbyWifiDevices,
    ].request();
    final denied = statuses.entries
        .where((e) => e.value.isPermanentlyDenied || (e.key == Permission.location && !e.value.isGranted))
        .map((e) => e.key.toString().replaceAll('Permission.', ''))
        .toList();
    if (!await Geolocator.isLocationServiceEnabled()) {
      denied.add('location service (turn on GPS)');
    }
    if (!mounted) return;
    setState(() => permissionProblem = denied.isEmpty ? null : 'Needs: ${denied.join(', ')}');
    await mesh.start();
  }

  Future<Position?> _position() async {
    try {
      return await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(accuracy: LocationAccuracy.high, timeLimit: Duration(seconds: 12)),
      );
    } catch (_) {
      try {
        return await Geolocator.getLastKnownPosition();
      } catch (_) {
        return null;
      }
    }
  }

  Future<void> _openSosForm() async {
    final result = await showModalBottomSheet<_SosForm>(
      context: context,
      isScrollControlled: true,
      builder: (_) => const SosSheet(),
    );
    if (result == null) return;
    final pos = await _position();
    final p = await mesh.identity.newSos(
      text: result.toText(),
      people: result.people,
      needs: result.needs(),
      lat: pos?.latitude,
      lng: pos?.longitude,
    );
    await mesh.sendSos(p);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(mesh.online
          ? 'SOS sent to the control room'
          : 'No network - SOS is hopping through ${mesh.peers.length} nearby phone(s)'),
    ));
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(
        title: const Text('ResQNet'),
        actions: [IconButton(icon: const Icon(Icons.settings), onPressed: _settings)],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (permissionProblem != null)
            Card(
              color: Colors.amber.shade100,
              child: ListTile(
                leading: const Icon(Icons.warning_amber),
                title: Text(permissionProblem!),
                trailing: TextButton(onPressed: openAppSettings, child: const Text('Settings')),
              ),
            ),
          _statusCard(cs),
          const SizedBox(height: 16),
          SizedBox(
            height: 150,
            child: FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: const Color(0xFFD32F2F),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24)),
              ),
              onPressed: _openSosForm,
              child: const Column(mainAxisSize: MainAxisSize.min, children: [
                Text('SOS', style: TextStyle(fontSize: 48, fontWeight: FontWeight.w900, color: Colors.white)),
                Text('సహాయం · மீட்பு · मदद', style: TextStyle(fontSize: 16, color: Colors.white)),
              ]),
            ),
          ),
          const SizedBox(height: 16),
          ..._mySos(),
          _shelters(),
          ExpansionTile(
            title: const Text('Mesh activity'),
            children: mesh.logs.take(40).map((l) => Align(
                  alignment: Alignment.centerLeft,
                  child: Padding(padding: const EdgeInsets.symmetric(vertical: 2), child: Text(l, style: const TextStyle(fontSize: 12))),
                )).toList(),
          ),
        ],
      ),
    );
  }

  Widget _statusCard(ColorScheme cs) {
    final online = mesh.online;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(online ? Icons.cloud_done : Icons.cloud_off, color: online ? Colors.green : Colors.orange),
            const SizedBox(width: 8),
            Text(online ? 'Connected to control room' : 'No internet - mesh mode', style: const TextStyle(fontWeight: FontWeight.bold)),
          ]),
          const SizedBox(height: 8),
          Wrap(spacing: 8, runSpacing: 4, children: [
            Chip(avatar: const Icon(Icons.bluetooth, size: 18), label: Text('${mesh.peers.length} phones nearby')),
            Chip(avatar: const Icon(Icons.inbox, size: 18), label: Text('${mesh.store.values.where((p) => p.type == 'SOS').length} SOS held')),
          ]),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Relay other people\'s SOS'),
            subtitle: const Text('Uses some battery. Keep the app open.'),
            value: mesh.relayEnabled,
            onChanged: (v) => mesh.setRelay(v),
          ),
        ]),
      ),
    );
  }

  List<Widget> _mySos() {
    if (mesh.mine.isEmpty) return [];
    return [
      const Text('My SOS', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
      ...mesh.mine.reversed.take(5).map((id) {
        final st = mesh.statuses[id];
        final delivered = mesh.acked.contains(id);
        return Card(
          color: st != null && st.version >= 3 ? Colors.blue.shade50 : null,
          child: ListTile(
            leading: Icon(st != null && st.version >= 3 ? Icons.directions_boat : delivered ? Icons.check_circle : Icons.schedule),
            title: Text(st?.text ?? (delivered ? 'Delivered to control room' : 'Waiting - hopping through the mesh')),
            subtitle: Text(id),
          ),
        );
      }),
    ];
  }

  Widget _shelters() {
    if (mesh.shelters.isEmpty) return const SizedBox.shrink();
    return ExpansionTile(
      title: const Text('Nearest shelters (saved offline)'),
      children: mesh.shelters.take(8).map((s) => ListTile(
            dense: true,
            leading: const Icon(Icons.home),
            title: Text('${s['name']}'),
            subtitle: Text('${(s['lat'] as num).toStringAsFixed(4)}, ${(s['lng'] as num).toStringAsFixed(4)}'),
          )).toList(),
    );
  }

  void _settings() {
    final ctl = TextEditingController(text: mesh.server);
    showDialog(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Settings'),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(controller: ctl, decoration: const InputDecoration(labelText: 'Control room server URL')),
          const SizedBox(height: 12),
          Text('Device: ${mesh.identity.deviceId}', style: const TextStyle(fontSize: 12)),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () {
              mesh.setServer(ctl.text);
              Navigator.pop(context);
            },
            child: const Text('Save'),
          ),
        ],
      ),
    );
  }
}

class _SosForm {
  int people = 1;
  bool medical = false, children = false, elderly = false, pregnant = false, disabled = false;
  bool food = false, water = false;
  String waterLevel = 'unknown';
  String note = '';

  List<String> needs() => [
        if (medical) 'medicine',
        if (food) 'food',
        if (water) 'water',
        'boat',
      ];

  /// Plain-language text so the AI triage (and humans) can read it in any channel.
  String toText() {
    final parts = <String>['$people people'];
    const levels = {'roof': 'we are on the roof', 'neck': 'water up to neck level', 'waist': 'water at waist level', 'ground': 'ground floor flooded'};
    if (levels.containsKey(waterLevel)) parts.add(levels[waterLevel]!);
    if (medical) parts.add('medical emergency');
    if (children) parts.add('children with us');
    if (elderly) parts.add('elderly person with us');
    if (pregnant) parts.add('pregnant woman');
    if (disabled) parts.add('disabled person');
    if (food) parts.add('no food');
    if (water) parts.add('need drinking water');
    if (note.trim().isNotEmpty) parts.add(note.trim());
    return parts.join('. ');
  }
}

class SosSheet extends StatefulWidget {
  const SosSheet({super.key});
  @override
  State<SosSheet> createState() => _SosSheetState();
}

class _SosSheetState extends State<SosSheet> {
  final f = _SosForm();

  Widget _check(String label, bool v, void Function(bool) set) =>
      FilterChip(label: Text(label), selected: v, onSelected: (x) => setState(() => set(x)));

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(left: 16, right: 16, top: 16, bottom: MediaQuery.of(context).viewInsets.bottom + 16),
      child: SingleChildScrollView(
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Send SOS', style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          Row(children: [
            const Text('People: '),
            IconButton(onPressed: () => setState(() => f.people = max(1, f.people - 1)), icon: const Icon(Icons.remove_circle_outline)),
            Text('${f.people}', style: const TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
            IconButton(onPressed: () => setState(() => f.people++), icon: const Icon(Icons.add_circle_outline)),
          ]),
          Wrap(spacing: 6, runSpacing: 6, children: [
            _check('Medical emergency', f.medical, (x) => f.medical = x),
            _check('Children', f.children, (x) => f.children = x),
            _check('Elderly', f.elderly, (x) => f.elderly = x),
            _check('Pregnant', f.pregnant, (x) => f.pregnant = x),
            _check('Disabled', f.disabled, (x) => f.disabled = x),
            _check('No food', f.food, (x) => f.food = x),
            _check('Need drinking water', f.water, (x) => f.water = x),
          ]),
          const SizedBox(height: 8),
          DropdownButtonFormField<String>(
            value: f.waterLevel,
            decoration: const InputDecoration(labelText: 'Where is the water?'),
            items: const [
              DropdownMenuItem(value: 'unknown', child: Text('Not sure')),
              DropdownMenuItem(value: 'ground', child: Text('Ground floor flooded')),
              DropdownMenuItem(value: 'waist', child: Text('Waist level')),
              DropdownMenuItem(value: 'neck', child: Text('Neck level')),
              DropdownMenuItem(value: 'roof', child: Text('We are on the roof')),
            ],
            onChanged: (v) => setState(() => f.waterLevel = v ?? 'unknown'),
          ),
          TextField(
            decoration: const InputDecoration(labelText: 'Short note (any language)', hintText: 'e.g. grandmother needs insulin'),
            maxLength: 160,
            onChanged: (v) => f.note = v,
          ),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              style: FilledButton.styleFrom(backgroundColor: const Color(0xFFD32F2F), padding: const EdgeInsets.all(16)),
              onPressed: () => Navigator.pop(context, f),
              child: const Text('SEND SOS', style: TextStyle(fontSize: 18, color: Colors.white)),
            ),
          ),
        ]),
      ),
    );
  }
}
