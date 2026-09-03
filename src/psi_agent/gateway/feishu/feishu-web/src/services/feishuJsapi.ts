/**
 * 调用其它 H5 JSAPI 前的 ``window.tt.config`` 初始化。
 *
 * 免登用的 ``requestAccess`` / ``requestAuthCode`` 不需要 config; 需要调用扫码、云文档、
 * ``openSchema`` 等 JSAPI 时, 先调本模块, 再由业务代码调用对应 ``tt.*`` API。
 *
 * 签名参数由后端 ``GET /feishu/jsapi/config`` 提供, App Secret 与 jsapi_ticket 不出服务端。
 */

import { getFeishuJsapiConfig } from "../api";
import { sdkReady } from "./feishuAuth";

/** JSSDK 不存在或当前版本不支持 ``tt.config``。调用方应显示重试而非继续调用 JSAPI。 */
export class FeishuJsapiUnavailable extends Error {}

export async function configureFeishuJsapi(jsApiList: readonly string[] = []): Promise<void> {
  await sdkReady();
  const config = await getFeishuJsapiConfig();
  const fn = window.tt?.config;
  if (!fn) {
    throw new FeishuJsapiUnavailable("JSSDK 不支持 tt.config");
  }
  return new Promise<void>((resolve, reject) => {
    fn({
      appId: config.appId,
      timestamp: config.timestamp,
      nonceStr: config.nonceStr,
      signature: config.signature,
      jsApiList: [...jsApiList],
      success: () => resolve(),
      fail: (err) =>
        reject(
          new Error(
            `tt.config 失败 (errno=${err.errno ?? "?"}): ${err.errString ?? ""}`,
          ),
        ),
    });
  });
}
