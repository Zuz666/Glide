import type { SpeechFeature } from "../../shared/speech-markup.js";

// Authored sample text: localization never sends XML to a model or changes a user's draft.
const TEXT: Record<string, readonly string[]> = {
  en: ["Take a moment to recover.", "Move to the", "safe path", "before continuing.", "Ready for the next step.", "Choose the", "left", "path.", "Recover first.", "Then continue.", "Follow the road.", "Stop here.", "Now continue."],
  ru: ["Немного отдохните и восстановите силы.", "Выберите", "безопасный путь", "перед продолжением.", "Всё готово к следующему шагу.", "Выберите", "левую", "тропу.", "Сначала восстановите силы.", "Затем продолжайте.", "Следуйте по дороге.", "Остановитесь здесь.", "Теперь продолжайте."],
  de: ["Nimm dir Zeit, dich zu erholen.", "Wähle den", "sicheren Weg", "bevor du weitergehst.", "Bereit für den nächsten Schritt.", "Wähle den", "linken", "Weg.", "Erhole dich zuerst.", "Dann geh weiter.", "Folge der Straße.", "Halte hier an.", "Geh jetzt weiter."],
  fr: ["Prenez un moment pour récupérer.", "Choisissez le", "chemin sûr", "avant de continuer.", "Tout est prêt pour la prochaine étape.", "Choisissez le chemin", "de gauche", ".", "Récupérez d’abord.", "Puis continuez.", "Suivez la route.", "Arrêtez-vous ici.", "Continuez maintenant."],
  es: ["Descansa un momento para recuperarte.", "Elige el", "camino seguro", "antes de continuar.", "Todo listo para el siguiente paso.", "Elige el camino de la", "izquierda", ".", "Recupérate primero.", "Luego continúa.", "Sigue el camino.", "Detente aquí.", "Ahora continúa."],
  pt: ["Descanse um pouco para recuperar as forças.", "Escolha o", "caminho seguro", "antes de continuar.", "Tudo pronto para o próximo passo.", "Escolha o caminho à", "esquerda", ".", "Recupere as forças primeiro.", "Depois continue.", "Siga a estrada.", "Pare aqui.", "Agora continue."],
  it: ["Riposa un momento per recuperare le forze.", "Scegli il", "percorso sicuro", "prima di continuare.", "Tutto pronto per il prossimo passo.", "Scegli il percorso a", "sinistra", ".", "Recupera prima le forze.", "Poi continua.", "Segui la strada.", "Fermati qui.", "Ora continua."],
  ko: ["잠시 쉬면서 회복하세요.", "계속하기 전에", "안전한 길을", "선택하세요.", "다음 단계로 갈 준비가 되었습니다.", "갈림길에서", "왼쪽", "길을 선택하세요.", "먼저 회복하세요.", "그런 다음 계속하세요.", "길을 따라가세요.", "여기서 멈추세요.", "이제 계속하세요."],
  zh: ["休息片刻，恢复体力。", "继续前进之前，请选择", "安全的道路", "。", "下一步已经准备就绪。", "请选择", "左边", "的道路。", "先恢复体力。", "然后继续前进。", "沿着道路前进。", "在这里停下。", "现在继续前进。"],
};
const LOCALES: Record<string, string> = { en: "en-US", ru: "ru-RU", de: "de-DE", fr: "fr-FR", es: "es-ES", pt: "pt-PT", it: "it-IT", ko: "ko-KR", zh: "zh-CN" };

export const VOICE_EXAMPLES: { id: string; label: string; plainLabel: string; features: SpeechFeature[] }[] = [
  { id: "rate-phrase", label: "Rate — whole phrase", plainLabel: "Recovery", features: ["rate"] },
  { id: "rate-span", label: "Rate — one passage", plainLabel: "Safe path", features: ["rate"] },
  { id: "pitch-phrase", label: "Pitch — whole phrase", plainLabel: "Next step", features: ["pitch"] },
  { id: "pitch-span", label: "Pitch — one word", plainLabel: "Direction", features: ["pitch"] },
  { id: "boundaries", label: "Sentence and paragraph boundaries", plainLabel: "Instructions", features: ["boundaries"] },
  { id: "pause", label: "Native pause (may be ignored)", plainLabel: "Stop and continue", features: ["pauses"] },
  { id: "combined", label: "Combined rate and pitch — phrase and span", plainLabel: "Recovery and route", features: ["rate", "pitch"] },
];

export function voiceExample(id: string, language: string, mode: "plain" | "ssml"): string {
  const code = Object.hasOwn(TEXT, language) ? language : "en";
  const t = TEXT[code].map((value) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"));
  const bodies: Record<string, string> = {
    "rate-phrase": `<prosody rate="slow">${t[0]}</prosody>`,
    "rate-span": `${t[1]} <prosody rate="slow">${t[2]}</prosody> ${t[3]}`,
    "pitch-phrase": `<prosody pitch="high">${t[4]}</prosody>`,
    "pitch-span": `${t[5]} <prosody pitch="low">${t[6]}</prosody> ${t[7]}`,
    boundaries: `<p><s>${t[8]}</s> <s>${t[9]}</s></p> <p>${t[10]}</p>`,
    pause: `${t[11]}<break time="750ms"/> ${t[12]}`,
    combined: `<prosody rate="slow" pitch="low">${t[8]}</prosody> ${t[9]} <prosody rate="fast" pitch="high">${t[10]}</prosody>`,
  };
  const body = bodies[id];
  if (!body) return "";
  const ssml = `<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="${LOCALES[code]}">${body}</speak>`;
  return mode === "ssml" ? ssml : new DOMParser().parseFromString(ssml, "application/xml").documentElement.textContent || "";
}
