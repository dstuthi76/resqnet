/**
 * ResQNet - flood duration per zone from Sentinel-1 radar (Google Earth Engine Code Editor)
 *
 * 1. Open https://code.earthengine.google.com (sign in with the Google account you registered for Earth Engine)
 * 2. New script -> paste this whole file -> Run
 * 3. Map shows the flood extent; the Console prints JSON.
 *    Copy the JSON into  data/flood_readings.json  in the ResQNet repo.
 *    (The Python version, flood_zones.py, writes that file for you.)
 *
 * Method (standard SAR change detection):
 *   water reflects radar away from the satellite, so flooded land turns dark in VV backscatter.
 *   flooded pixel = after < -16 dB  AND  dropped > 3 dB vs. the dry-season baseline
 *                   AND not permanent water (JRC)  AND slope < 5 deg (SRTM)
 *   A zone counts as flooded on an acquisition when >= 5 % of it is flooded.
 *   hoursFlooded = time between the estimated start and end of flooding, using the midpoints
 *   between Sentinel-1 passes (passes are days apart, so this is an estimate - say so in the pitch).
 */
var EVENT_ID = 'HYD-2020-10';
var BEFORE = ['2020-09-01', '2020-10-10'];   // dry-ish baseline
var AFTER = ['2020-10-12', '2020-11-10'];    // event window
var EVENT_START = Date.parse('2020-10-13T12:30:00Z'); // heavy rain began, 18:00 IST
var WATER_DB = -16, DROP_DB = 3, FRACTION_MIN = 0.05;
var DEFAULT_GAP_H = 6 * 24;

var ZONES = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"zoneId": "HYD-MLK", "name": "Malakpet", "lowLying": true}, "geometry": {"type": "Polygon", "coordinates": [[[78.49056, 17.36399], [78.50944, 17.36399], [78.50944, 17.38201], [78.49056, 17.38201], [78.49056, 17.36399]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-CYG", "name": "Chandrayangutta", "lowLying": true}, "geometry": {"type": "Polygon", "coordinates": [[[78.47256, 17.30799], [78.49144, 17.30799], [78.49144, 17.32601], [78.47256, 17.32601], [78.47256, 17.30799]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-FLK", "name": "Falaknuma", "lowLying": true}, "geometry": {"type": "Polygon", "coordinates": [[[78.45756, 17.32199], [78.47644, 17.32199], [78.47644, 17.34001], [78.45756, 17.34001], [78.45756, 17.32199]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-TLC", "name": "Tolichowki", "lowLying": true}, "geometry": {"type": "Polygon", "coordinates": [[[78.40356, 17.38899], [78.42244, 17.38899], [78.42244, 17.40701], [78.40356, 17.40701], [78.40356, 17.38899]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-MDP", "name": "Mehdipatnam", "lowLying": false}, "geometry": {"type": "Polygon", "coordinates": [[[78.43056, 17.38599], [78.44944, 17.38599], [78.44944, 17.40401], [78.43056, 17.40401], [78.43056, 17.38599]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-LBN", "name": "LB Nagar", "lowLying": true}, "geometry": {"type": "Polygon", "coordinates": [[[78.54256, 17.33799], [78.56144, 17.33799], [78.56144, 17.35601], [78.54256, 17.35601], [78.54256, 17.33799]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-SRN", "name": "Saroornagar", "lowLying": true}, "geometry": {"type": "Polygon", "coordinates": [[[78.52356, 17.34399], [78.54244, 17.34399], [78.54244, 17.36201], [78.52356, 17.36201], [78.52356, 17.34399]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-UPL", "name": "Uppal", "lowLying": false}, "geometry": {"type": "Polygon", "coordinates": [[[78.54956, 17.39199], [78.56844, 17.39199], [78.56844, 17.41001], [78.54956, 17.41001], [78.54956, 17.39199]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-HYT", "name": "Hayathnagar", "lowLying": false}, "geometry": {"type": "Polygon", "coordinates": [[[78.59456, 17.31799], [78.61344, 17.31799], [78.61344, 17.33601], [78.59456, 17.33601], [78.59456, 17.31799]]]}}, {"type": "Feature", "properties": {"zoneId": "HYD-MRB", "name": "Moosarambagh", "lowLying": true}, "geometry": {"type": "Polygon", "coordinates": [[[78.50356, 17.36299], [78.52244, 17.36299], [78.52244, 17.38101], [78.50356, 17.38101], [78.50356, 17.36299]]]}}]};
var zones = ee.FeatureCollection(ZONES);
var aoi = zones.geometry().bounds().buffer(3000);

var s1 = ee.ImageCollection('COPERNICUS/S1_GRD')
  .filterBounds(aoi)
  .filter(ee.Filter.eq('instrumentMode', 'IW'))
  .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
  .select('VV');

function smooth(img) {
  return img.focalMedian(50, 'circle', 'meters').copyProperties(img, ['system:time_start']);
}
var before = s1.filterDate(BEFORE[0], BEFORE[1]).map(smooth).median();
var slope = ee.Terrain.slope(ee.Image('USGS/SRTMGL1_003'));
var permanent = ee.Image('JRC/GSW1_4/GlobalSurfaceWater').select('seasonality').gte(10).unmask(0);

function floodMask(img) {
  var a = smooth(img);
  return a.lt(WATER_DB)
    .and(before.subtract(a).gt(DROP_DB))
    .and(before.gt(WATER_DB))
    .and(slope.lt(5))
    .and(permanent.not())
    .rename('flood');
}

var afterCol = s1.filterDate(AFTER[0], AFTER[1]);
print('Sentinel-1 passes in event window', afterCol.size());

var obs = afterCol.map(function (img) {
  var millis = img.date().millis();
  return floodMask(img).reduceRegions({collection: zones, reducer: ee.Reducer.mean(), scale: 30})
    .map(function (f) { return f.set('millis', millis); });
}).flatten();

// Map: max flood extent during the event
var maxFlood = afterCol.map(floodMask).max().selfMask();
Map.centerObject(zones, 12);
Map.addLayer(before, {min: -25, max: 0}, 'Baseline VV (dB)', false);
Map.addLayer(maxFlood, {palette: ['0044ff']}, 'Max flood extent');
Map.addLayer(zones.style({color: 'ff0000', fillColor: '00000000'}), {}, 'ResQNet zones');

obs.select(['zoneId', 'mean', 'flood', 'millis'], null, false).evaluate(function (fc, err) {
  if (err) { print('Error', err); return; }
  var byZone = {};
  fc.features.forEach(function (f) {
    var p = f.properties;
    var frac = (p.mean !== undefined && p.mean !== null) ? p.mean : p.flood;
    if (frac === null || frac === undefined) return;          // pass did not cover this zone
    (byZone[p.zoneId] = byZone[p.zoneId] || []).push({t: p.millis, f: frac});
  });
  var readings = ZONES.features.map(function (z) {
    var id = z.properties.zoneId;
    var o = (byZone[id] || []).sort(function (a, b) { return a.t - b.t; });
    // merge passes on the same day (adjacent tiles)
    var merged = [];
    o.forEach(function (x) {
      var last = merged[merged.length - 1];
      if (last && x.t - last.t < 3 * 3600e3) { last.f = Math.max(last.f, x.f); } else { merged.push({t: x.t, f: x.f}); }
    });
    var floodedIdx = [];
    merged.forEach(function (x, i) { if (x.f >= FRACTION_MIN) floodedIdx.push(i); });
    var hours = 0, maxFrac = 0;
    merged.forEach(function (x) { maxFrac = Math.max(maxFrac, x.f); });
    if (floodedIdx.length) {
      var first = floodedIdx[0], last = floodedIdx[floodedIdx.length - 1];
      var start = first > 0 ? Math.max(EVENT_START, (merged[first - 1].t + merged[first].t) / 2) : Math.max(EVENT_START, merged[first].t - DEFAULT_GAP_H * 1800e3);
      var end = last < merged.length - 1 ? (merged[last].t + merged[last + 1].t) / 2 : merged[last].t + DEFAULT_GAP_H * 1800e3;
      hours = Math.max(0, Math.round((end - start) / 3600e3));
    }
    return {
      zoneId: id, hoursFlooded: hours, floodedFraction: Math.round(maxFrac * 1000) / 1000,
      observations: merged.map(function (x) { return {date: new Date(x.t).toISOString(), fraction: Math.round(x.f * 1000) / 1000}; })
    };
  });
  var out = {eventId: EVENT_ID, source: 'Sentinel-1 GRD via Google Earth Engine', generated: new Date().toISOString(), readings: readings};
  print('Copy this into data/flood_readings.json:');
  print(JSON.stringify(out, null, 1));
});
