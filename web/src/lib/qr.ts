import QRCode from "qrcode";

export async function renderQrSvg(text: string, size = 256): Promise<string> {
  return QRCode.toString(text, {
    type: "svg",
    margin: 1,
    width: size,
    errorCorrectionLevel: "M",
  });
}
