import CoreAudio
import Foundation
// setaudio <output device name> <input device name>: makes them the Mac's default output (and system
// output) and default input, for cell_bridge.sh. Build on the Mac Mini:
//   swiftc -O -o ~/.baresip-cell/bin/setaudio cell_setaudio.swift
func devices() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids
}
func name(_ id: AudioDeviceID) -> String {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var cf: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &cf)
    return cf?.takeRetainedValue() as String? ?? ""
}
func setDefault(_ sel: AudioObjectPropertySelector, _ want: String) -> Bool {
    guard var id = devices().first(where: { name($0) == want }) else { return false }
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    return AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &id) == noErr
}
let a = CommandLine.arguments
guard a.count == 3 else {
    FileHandle.standardError.write("usage: setaudio <output device name> <input device name>\n".data(using: .utf8)!)
    exit(2)
}
let ok = setDefault(kAudioHardwarePropertyDefaultOutputDevice, a[1]) && setDefault(kAudioHardwarePropertyDefaultSystemOutputDevice, a[1]) && setDefault(kAudioHardwarePropertyDefaultInputDevice, a[2])
print(ok ? "ok" : "failed"); exit(ok ? 0 : 1)
