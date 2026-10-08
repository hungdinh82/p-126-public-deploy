// Cabin music for the demo: a calm, warm loop synthesized with Web Audio, so
// it needs no audio file or licence. Four bars of Cmaj7–Am7–Fmaj7–G at
// 80 BPM: soft pads, a slow arpeggio and a gentle lead, with no drums.
const BPM = 80;
const STEP = 60 / BPM / 4; // one sixteenth note
const BAR = 16;
const VOLUME = .28;
// Phase-driven ducking: the microphone must not hear the music, and ViVi's
// voice stays clear over it.
const DUCK = { listening: 0, transcribing: .25, speaking: .3, synthesizing: .5 };

const note = name => {
  const [, letter, accidental, octave] = name.match(/^([A-G])(#?)(\d)$/);
  const semitone = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 }[letter] + (accidental ? 1 : 0);
  return 440 * 2 ** ((semitone + (Number(octave) + 1) * 12 - 69) / 12);
};
const CHORDS = [
  { bass: 'C2', tones: ['E3', 'G3', 'B3', 'D4'] },
  { bass: 'A1', tones: ['C3', 'E3', 'G3', 'C4'] },
  { bass: 'F1', tones: ['A3', 'C4', 'E4', 'G4'] },
  { bass: 'G1', tones: ['B3', 'D4', 'G4', 'A4'] }
];
// [sixteenth within the 4 bars, note, length in sixteenths]: long, sparse notes.
const MELODY = [
  [0, 'E5', 6], [8, 'D5', 4], [12, 'G4', 4],
  [16, 'C5', 8], [26, 'E5', 6],
  [32, 'A4', 6], [40, 'C5', 4], [44, 'G4', 4],
  [48, 'B4', 8], [58, 'D5', 6]
];

export function createCabinMusic() {
  let context = null;
  let master = null;
  let echo = null;
  let timer = null;
  let step = 0;
  let nextTime = 0;
  let level = 1;

  function setup() {
    context = new AudioContext();
    master = context.createGain();
    master.gain.value = 0;
    const tone = context.createBiquadFilter();
    tone.type = 'lowpass'; tone.frequency.value = 4500;
    master.connect(tone).connect(context.destination);
    // A soft dotted-eighth echo gives the room some air.
    echo = context.createGain();
    echo.gain.value = .35;
    const delay = context.createDelay(2);
    delay.delayTime.value = STEP * 3;
    const feedback = context.createGain();
    feedback.gain.value = .38;
    const damp = context.createBiquadFilter();
    damp.type = 'lowpass'; damp.frequency.value = 2200;
    echo.connect(delay).connect(damp).connect(feedback).connect(delay);
    damp.connect(master);
  }

  function voice(type, frequency, start, length, gain, cutoff = 0, attack = .01) {
    const osc = context.createOscillator();
    const env = context.createGain();
    osc.type = type; osc.frequency.value = frequency;
    env.gain.setValueAtTime(0, start);
    env.gain.linearRampToValueAtTime(gain, start + attack);
    env.gain.exponentialRampToValueAtTime(.0001, start + length);
    let output = osc.connect(env);
    if (cutoff) {
      const filter = context.createBiquadFilter();
      filter.type = 'lowpass'; filter.frequency.value = cutoff;
      output = output.connect(filter);
    }
    output.connect(master);
    output.connect(echo);
    osc.start(start); osc.stop(start + length + .02);
  }

  function schedule(index, time) {
    const beat = index % BAR;
    const chord = CHORDS[Math.floor(index / BAR) % CHORDS.length];
    const bar = STEP * BAR;
    if (beat === 0) {
      // Warm pad that swells in and lets go across the whole bar.
      chord.tones.forEach(tone => voice('triangle', note(tone), time, bar * 1.1, .05, 1400, .6));
      voice('sine', note(chord.bass), time, bar, .3, 0, .08);
    }
    // A slow, music-box arpeggio on every eighth note.
    if (beat % 2 === 0) voice('sine', note(chord.tones[[0, 1, 2, 3, 2, 1, 2, 3][beat / 2]]) * 2, time, STEP * 5, .05, 0, .02);
    const lead = MELODY.find(([at]) => at === index % (BAR * CHORDS.length));
    if (lead) voice('triangle', note(lead[1]), time, lead[2] * STEP, .07, 2500, .08);
  }

  function pump() {
    // Look ahead 120 ms so timer jitter never reaches the audio clock.
    while (nextTime < context.currentTime + .12) {
      schedule(step, nextTime);
      step += 1;
      nextTime += STEP;
    }
  }

  function applyLevel(seconds = .25) {
    if (!context) return;
    const target = timer ? VOLUME * level : 0;
    master.gain.cancelScheduledValues(context.currentTime);
    master.gain.setTargetAtTime(target, context.currentTime, seconds / 3);
  }

  return {
    async play() {
      if (timer) return;
      if (!context) setup();
      if (context.state === 'suspended') await context.resume();
      step = 0;
      nextTime = context.currentTime + .05;
      timer = setInterval(pump, 25);
      pump();
      applyLevel(.6);
    },
    stop() {
      if (!timer) return;
      clearInterval(timer);
      timer = null;
      applyLevel(.3);
    },
    duck(phase) {
      level = DUCK[phase] ?? 1;
      applyLevel(phase === 'listening' ? .08 : .4);
    }
  };
}
