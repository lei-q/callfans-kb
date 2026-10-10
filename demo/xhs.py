"""
小红书 发布视频脚本 - 从相册选择视频并发布

Features:
    - 打开发布页面 → 相册选择视频
    - 设置封面 → 跳过/保留 BGM
    - 填写标题和正文（支持 # 和 @ 标签）
    - 发布或保存草稿
    - 获取发布后的分享链接
"""
import sys
import os
import re
import random
import base64

from appium.webdriver.common.appiumby import AppiumBy
from appium.webdriver.extensions.android.nativekey import AndroidKey

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sma_autoui.engine import TaskRunner
from sma_autoui.driver import Driver, Device
from sma_autoui.common import logger, upload_image


class XHSVideoPublisher:
    """业务类：小红书发布视频"""

    def __init__(self, driver: Driver, job_id=None, **kwargs):
        self.driver = driver
        self.job_id = job_id

        # 业务参数
        self.folder_name = kwargs.get("folder_name", "Movies")
        self.images_nums = int(kwargs.get("images_nums", 1))
        self.enable_bgm = kwargs.get("enable_bgm", False)
        self.original = kwargs.get("original", False)
        self.draft = kwargs.get("draft", False)
        self.text = self._decode_from_base64(kwargs.get("text", ""))
        self.content = self._decode_from_base64(kwargs.get("content", ""))

    @staticmethod
    def _decode_from_base64(base64_text):
        """解码 base64 文本"""
        try:
            return base64.b64decode(base64_text).decode('utf-8')
        except Exception:
            return base64_text

    def _send_keys_with_newline(self, element, text):
        """处理包含 # 和 @ 标签的文本输入"""
        text = text.replace("\\n", "\n")

        if '#' in text:
            parts = text.split('#', 1)
            before_hash = parts[0]
            after_hash = '#' + parts[1]

            if before_hash:
                element.send_keys(before_hash)
                self.driver.random_wait(1)
                self.driver.press_keycode(AndroidKey.SPACE)

            if "@" in after_hash:
                at_parts = after_hash.split('@', 1)
                before_at = at_parts[0]
                after_at = at_parts[1]

                # 逐个字符粘贴 # 和 @ 之间的内容
                for char in before_at:
                    self.driver.driver.set_clipboard_text(char)
                    self.driver.random_wait(1, 2)
                    self.driver.press_keycode(50, 28672)  # PASTE
                    self.driver.random_wait(3, 5)

                # 空格
                self.driver.press_keycode(AndroidKey.SPACE)

                # 粘贴 @ 内容
                self.driver.driver.set_clipboard_text("@" + after_at + "好")
                self.driver.press_keycode(50, 28672)
                self.driver.press_keycode(67)  # BACKSPACE 删除多余的"好"
                self.driver.random_wait(8, 11)

                # 点击 @ 建议
                title = self.driver.smart_find(
                    locators=[
                        (AppiumBy.XPATH, f'//android.widget.TextView[@text="{after_at}"]'),
                    ],
                    timeout=5,
                    intent=("选择推荐账号", "Click suggestion account"),
                )
                if title:
                    title.click()
                    self.driver.random_wait(2)

            else:
                # 只有 # 没有 @
                for char in after_hash:
                    self.driver.driver.set_clipboard_text(char)
                    self.driver.random_wait(1, 2)
                    self.driver.press_keycode(50, 28672)
                    self.driver.random_wait(3, 5)
        else:
            element.send_keys(text)
            self.driver.driver.set_clipboard_text(text)
            self.driver.random_wait(3, 5)

    def _open_publish_page(self):
        """打开发布页面"""
        with self.driver.step("打开发布页面", "Open Publish Page"):
            # 处理可能的"继续编辑"弹窗
            try:
                edit_draft = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("继续编辑")'),
                    ],
                    timeout=3,
                    intent=("检测编辑弹窗", "Detect edit dialog"),
                )
                if edit_draft:
                    cancel_btn = self.driver.smart_find(
                        locators=[
                            (AppiumBy.XPATH,
                             '(//android.widget.FrameLayout[@resource-id="com.xingin.xhs:id/0_resource_name_obfuscated"])[2]/android.widget.ImageView'),
                        ],
                        timeout=3,
                        intent=("关闭编辑弹窗", "Close edit dialog"),
                    )
                    if cancel_btn:
                        cancel_btn.click()
            except Exception:
                pass

            publish_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.XPATH, '//android.widget.RelativeLayout[@content-desc="发布"]'),
                ],
                timeout=5,
                intent=("点击发布按钮", "Click publish button"),
                critical=True,
            )
            publish_btn.click()
            self.driver.random_wait(2, 3)
            logger.info("已进入发布页面", "Entered publish page")

    def _select_album(self):
        """点击从相册选择"""
        with self.driver.step("选择相册", "Select Album"):
            album_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("从相册选择")'),
                ],
                timeout=5,
                intent=("点击从相册选择", "Click select from album"),
                critical=True,
            )
            album_btn.click()
            self.driver.random_wait(2, 3)

    def _choose_videos(self):
        """选择视频"""
        with self.driver.step("选择视频", "Choose Videos"):
            # 进入全部相册
            all_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("全部")'),
                ],
                timeout=5,
                intent=("点击全部", "Click all"),
                critical=True,
            )
            all_btn.click()
            self.driver.random_wait(2, 3)

            # 进入指定文件夹
            folder_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{self.folder_name}")'),
                ],
                timeout=5,
                intent=("选择文件夹", "Select folder"),
                critical=True,
            )
            folder_btn.click()
            self.driver.random_wait(2, 3)

            # 选择视频
            logger.info("准备选择 {} 个视频", "Selecting {} videos", self.images_nums)
            for i in range(self.images_nums):
                index = i * 2 + 4
                video_item = self.driver.smart_find(
                    locators=[
                        (AppiumBy.XPATH,
                         f'(//android.widget.ImageView[@resource-id="com.xingin.xhs:id/0_resource_name_obfuscated"])[{index}]'),
                    ],
                    timeout=5,
                    intent=("选择视频", "Select video"),
                )
                if video_item:
                    video_item.click()
                    self.driver.random_wait(1, 2)

    def _confirm_selected_videos(self):
        """确认已选视频"""
        with self.driver.step("确认视频选择", "Confirm Video Selection"):
            next_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.XPATH, '//android.widget.TextView[@content-desc="下一步"]'),
                ],
                timeout=5,
                intent=("点击下一步", "Click next"),
                critical=True,
            )
            next_btn.click()
            self.driver.random_wait(30)

    def _skip_background_music(self):
        """跳过背景音乐"""
        with self.driver.step("处理背景音乐", "Handle Background Music"):
            if not self.enable_bgm:
                delete_music = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().description("删除音乐")'),
                    ],
                    timeout=3,
                    intent=("删除背景音乐", "Delete background music"),
                )
                if delete_music:
                    delete_music.click()
                    logger.info("已取消背景音乐", "Background music removed")

            # 点击下一步
            next_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("下一步")'),
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("下一步")'),
                ],
                timeout=5,
                intent=("点击下一步", "Click next"),
                critical=True,
            )
            next_btn.click()
            self.driver.random_wait(6, 8)

    def _choose_cover(self):
        """选择封面"""
        with self.driver.step("选择封面", "Choose Cover"):
            cover_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("选封面")'),
                ],
                timeout=5,
                intent=("点击选封面", "Click choose cover"),
                critical=True,
            )
            cover_btn.click()
            self.driver.random_wait(4, 8)

    def _confirm_cover(self):
        """确认封面"""
        with self.driver.step("确认封面", "Confirm Cover"):
            next_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("下一步")'),
                ],
                timeout=5,
                intent=("确认封面下一步", "Confirm cover next"),
            )
            if next_btn:
                next_btn.click()
                self.driver.random_wait(5, 6)

            done_btn = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("完成")'),
                ],
                timeout=5,
                intent=("点击完成", "Click done"),
            )
            if done_btn:
                done_btn.click()
            self.driver.random_wait(8, 10)

    def _fill_title_and_content(self):
        """填写标题和正文"""
        with self.driver.step("填写标题和正文", "Fill Title and Content"):
            # 填写标题
            title_input = self.driver.smart_find(
                locators=[
                    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("标题")'),
                ],
                timeout=5,
                intent=("点击标题输入框", "Click title input"),
                critical=True,
            )
            title_input.click()
            self.driver.random_wait(1)
            title_input.send_keys(self.text)
            logger.info("标题已填写", "Title filled")

            # 填写正文
            content_group = self.driver.smart_find(
                locators=[
                    (AppiumBy.XPATH,
                     '(//android.widget.ScrollView[@resource-id="com.xingin.xhs:id/0_resource_name_obfuscated"])[2]'),
                ],
                timeout=5,
                intent=("定位正文区域", "Locate content area"),
                critical=True,
            )
            content_input = content_group.find_element(AppiumBy.XPATH, '//android.widget.EditText')
            content_input.click()
            self.driver.random_wait(1)

            self._send_keys_with_newline(content_input, self.content)
            logger.info("正文已填写", "Content filled")

    def _submit_video(self):
        """提交视频"""
        with self.driver.step("提交视频", "Submit Video"):
            if self.draft:
                draft_btn = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("草稿")'),
                    ],
                    timeout=5,
                    intent=("点击保存草稿", "Click save draft"),
                    critical=True,
                )
                draft_btn.click()

                confirm_btn = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("确定")'),
                    ],
                    timeout=5,
                    intent=("确认草稿", "Confirm draft"),
                    critical=True,
                )
                confirm_btn.click()
                logger.info("视频已保存为草稿", "Video saved as draft")
            else:
                pub_btn = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("发布")'),
                    ],
                    timeout=5,
                    intent=("点击发布", "Click publish"),
                    critical=True,
                )
                pub_btn.click()

                no_declaration_btn = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("我的内容无需声明")'),
                    ],
                    timeout=5,
                    intent=("点击我的内容无需声明", "Click my content no declaration"),
                )

                if no_declaration_btn:
                    no_declaration_btn.click()

                # 二次确认发布
                pub_btn2 = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("发布")'),
                    ],
                    timeout=5,
                    intent=("二次确认发布", "Double confirm publish"),
                )
                if pub_btn2:
                    pub_btn2.click()
                self.driver.random_wait(8, 10)
                # ai_generated_btn = self.driver.smart_find(
                #     locators=[
                #         (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("合成内容")'),
                #     ],
                #     timeout=5,
                #     intent=("点击含 AI 合成内容", "Click AI generated content"),
                # )
                # if ai_generated_btn:
                #     ai_generated_btn.click()

                logger.info("视频已提交发布", "Video submitted for publishing")

            self.driver.random_wait(60, 90)

    def _get_invitation_link(self):
        """获取发布后的分享链接"""
        with self.driver.step("获取分享链接", "Get Share Link"):
            # 点击刚发布的视频
            first_post = self.driver.smart_find(
                locators=[
                    (AppiumBy.XPATH,
                     '(//android.widget.ImageView[@resource-id="com.xingin.xhs:id/0_resource_name_obfuscated"])[1]'),
                ],
                timeout=5,
                intent=("点击刚发布的视频", "Click published video"),
            )
            if first_post:
                first_post.click()
                self.driver.random_wait(15, 20)

                # 截图上传
                screenshot_b64 = self.driver.take_screenshot()
                if screenshot_b64:
                    upload_image("视频播放页", screenshot_b64, self.job_id)

                # 点击分享 → 复制链接
                share_btn = self.driver.smart_find(
                    locators=[
                        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("分享")'),
                    ],
                    timeout=5,
                    intent=("点击分享", "Click share"),
                )
                if share_btn:
                    share_btn.click()
                    self.driver.random_wait(2)

                    # 左滑找复制链接
                    edit_btn = self.driver.smart_find(
                        locators=[
                            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("编辑")'),
                        ],
                        timeout=3,
                        intent=("定位编辑按钮", "Locate edit button"),
                    )
                    if edit_btn:
                        loc = edit_btn.location
                        sz = edit_btn.size
                        cx = loc['x'] + sz['width'] / 2
                        cy = loc['y'] + sz['height'] / 2
                        self.driver.driver.swipe(cx, cy, cx * 0.6, cy, 300)
                        self.driver.driver.swipe(cx, cy, cx * 0.6, cy, 300)

                    copy_link = self.driver.smart_find(
                        locators=[
                            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("复制链接")'),
                        ],
                        timeout=5,
                        intent=("点击复制链接", "Click copy link"),
                    )
                    if copy_link:
                        copy_link.click()
                        logger.info("已获取视频笔记分享链接", "Video share link copied")

    def run_flow(self):
        """主业务流"""

        # 检查登录状态
        with self.driver.step("检查登录状态", "Check Login Status"):
            self.driver.smart_find(
                locators=[
                    (AppiumBy.XPATH, '//android.view.ViewGroup[@content-desc="我"]'),
                ],
                timeout=30,
                intent=("检查登录状态", "Check login status"),
                critical=True,
            )

        # 打开发布页面
        self._open_publish_page()

        # 选择相册
        self._select_album()

        # 选择视频
        self._choose_videos()

        # 确认选择
        self._confirm_selected_videos()

        # 处理背景音乐
        self._skip_background_music()

        # 选择封面
        self._choose_cover()

        # 确认封面
        self._confirm_cover()

        # 填写标题和正文
        self._fill_title_and_content()

        # 提交发布
        self._submit_video()

        # 获取分享链接（非草稿模式）
        if not self.draft:
            self._get_invitation_link()
        # 清理文件
        self._cleanup_files()

    def _cleanup_files(self):
        """清理素材文件"""
        with self.driver.step("清理素材文件", "Clean Up Media Files"):
            try:
                clean_cmd = f"shell rm -rf /sdcard/{self.folder_name}/*"
                self.driver.device.adb.execute(clean_cmd)
                logger.info("已清理素材文件", "Media files cleaned")
            except Exception as e:
                logger.warning("清理素材文件失败: {}", "Failed to clean media files: {}", e)

        logger.info("视频发布任务完成", "Video publishing task completed")


# ==============================================================
# 程序入口：组装底层引擎，驱动业务类运行
# ==============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='小红书 发布视频脚本')
    # 公共参数解析
    parser.add_argument('-j', '--job_id', dest='job_id', type=str, default=None, help='任务 ID')
    parser.add_argument('-u', '--appium_url', dest='appium_url', type=str, default=None, help='Appium 服务器地址')
    parser.add_argument('-d', '--deviceName', dest='deviceName', type=str, default=None, help='设备名称')
    parser.add_argument('-s', '--systemPort', dest='systemPort', type=str, default=None, help='系统端口')
    parser.add_argument('-a', '--adbPort', dest='adbPort', type=str, default=None, help='ADB 端口')
    parser.add_argument('-p', '--platformName', dest='platformName', type=str, default=None, help='平台名称')
    parser.add_argument('-ver', '--platformVersion', dest='platformVersion', type=str, default=None, help='平台版本')
    parser.add_argument('-v', '--variables', dest='variables', type=str, default='{}', help='变量')
    parser.add_argument('-accountId', '--accountId', dest='accountId', type=str, default=None, help='账号 ID')

    # 定制参数解析
    parser.add_argument('-folder_name', '--folder_name', dest='folder_name', type=str, default='Movies',
                        help='视频文件夹名')
    parser.add_argument('-images_nums', '--images_nums', type=int, default=1, help='Number of images')
    parser.add_argument('-text', '--text', dest='text', type=str, required=True, help='标题（Base64编码）')
    parser.add_argument('-content', '--content', dest='content', type=str, required=True, help='正文（Base64编码）')
    parser.add_argument('-enable_bgm', '--enable_bgm', dest='enable_bgm', type=int, default=0, help='是否启用BGM')
    parser.add_argument('-original', '--original', dest='original', type=int, default=0, help='是否原创声明')
    parser.add_argument('-draft', '--draft', dest='draft', type=int, default=0, help='是否保存草稿')
    parser.add_argument('-file', '--file', dest='file', type=str, required=False)

    args = parser.parse_args()

    APP_PACKAGES = {
        "XHS": ("com.xingin.xhs", ".index.v2.IndexActivityV2"),
        "INS": ("com.instagram.android", "com.instagram.android.activity.MainTabActivity"),
        "TK": ("com.zhiliaoapp.musically", "com.ss.android.ugc.aweme.splash.SplashActivity"),
        "YOUTUBE": ("com.google.android.youtube", "com.google.android.youtube.app.honeycomb.Shell$HomeActivity"),
        "WB": ("com.sina.weibo", ".VisitorMainTabActivity"),
        "DOUYIN": ("com.ss.android.ugc.aweme", ".splash.SplashActivity"),
        "FACEBOOK": ("com.facebook.katana", "com.facebook.katana.LoginActivity"),
        "KS": ("com.smile.gifmaker", "com.yxcorp.gifshow.HomeActivity")
    }

    package_info = APP_PACKAGES.get('XHS')

    # 1. 初始化环境
    device = Device(device_name=args.deviceName,adb_port=args.adbPort)
    desired_caps = {
        "platformName": "Android",
        "platformVersion": args.platformVersion,
        "deviceName": args.deviceName,
        "udid": args.deviceName,
        "automationName": "UiAutomator2",
        "appPackage": package_info[0],
        "appActivity": package_info[1],
        "noReset": True,
        "fullReset": False,
        "autoLaunch": True,
        "systemPort": args.systemPort,
        "adbPort": args.adbPort,
        "newCommandTimeout": 2400,
        "skipDeviceInitialization": True,
        "skipUnlock": True,
        "autoGrantPermissions": True,
        "disableWindowAnimation": True,
        "uiautomator2ServerLaunchTimeout": 300000,
    }
    task_name = "XHS Video Publisher"
    driver = Driver(device=device, appium_url=args.appium_url, job_id=args.job_id, desired_caps=desired_caps,
                    app_name="xhs", app_version="1.0.0", task_name=task_name)

    # 2. 实例化业务类
    publisher = XHSVideoPublisher(
        driver=driver,
        job_id=args.job_id,
        folder_name=args.folder_name,
        images_nums=args.images_nums,
        text=args.text,
        content=args.content,
        enable_bgm=args.enable_bgm != 0,
        original=args.original != 0,
        draft=args.draft != 0,
    )

    # 3. 交给健壮的引擎运行
    TaskRunner(driver=driver).run(
        task_func=publisher.run_flow,
        task_name=task_name
    )