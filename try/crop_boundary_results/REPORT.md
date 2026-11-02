# Q/O 裁剪边界对照

同一 PP-OCRv6 medium，batch_size=1，三张尾行，参考为 MDIWGQGTMVTVSS。

恢复底部使用原始图片中的真实像素；不对残基作替换。三条图片来自三个抗体，但该尾部文字相同，不能视为三种不同序列的广泛验证。

| 裁图条件 | 完全一致 |
|---|---:|
| 原裁图 | 0/3 |
| 只去右侧空白，保留原来的底部边界 | 3/3 |
| 只补8像素白边，不恢复原始笔画 | 0/3 |
| 从原图恢复底部8行像素，保留右侧空白 | 3/3 |
| 恢复底部8行像素，再去右侧空白 | 3/3 |
| 恢复底部、去右侧空白，再四周补8像素白边 | 3/3 |

将修正裁图识别出的尾行，与原先未改变的前两行识别输出拼接：完整序列有 3/3 条与冻结参考完全一致。没有人工将 O 替换成 Q。


## 边界诊断

```json
[
  {
    "item": "AB-1",
    "original_box": [
      699,
      710,
      2514,
      750
    ],
    "original_size": [
      1815,
      40
    ],
    "ink_bbox_in_original": [
      11,
      4,
      473,
      40
    ],
    "black_pixels_below_old_boundary": 44,
    "restored_pixels_rows": 8
  },
  {
    "item": "AB-2",
    "original_box": [
      697,
      912,
      2514,
      953
    ],
    "original_size": [
      1817,
      41
    ],
    "ink_bbox_in_original": [
      13,
      4,
      475,
      41
    ],
    "black_pixels_below_old_boundary": 32,
    "restored_pixels_rows": 8
  },
  {
    "item": "AB-5",
    "original_box": [
      697,
      1522,
      2514,
      1562
    ],
    "original_size": [
      1817,
      40
    ],
    "ink_bbox_in_original": [
      13,
      4,
      475,
      40
    ],
    "black_pixels_below_old_boundary": 45,
    "restored_pixels_rows": 8
  }
]
```

## AB-1 对照图片与输出

### 原裁图

输入尺寸：[1815, 40]；模型输出：`MDIWGOGTMVTVSS`。

![AB-1_original](images/AB-1_original.png)

### 只去右侧空白，保留原来的底部边界

输入尺寸：[481, 40]；模型输出：`MDIWGQGTMVTVSS`。

![AB-1_trim_right_only](images/AB-1_trim_right_only.png)

### 只补8像素白边，不恢复原始笔画

输入尺寸：[1815, 48]；模型输出：`MDIWGOGTMVTVSS`。

![AB-1_white_bottom_padding_only](images/AB-1_white_bottom_padding_only.png)

### 从原图恢复底部8行像素，保留右侧空白

输入尺寸：[1815, 48]；模型输出：`MDIWGQGTMVTVSS`。

![AB-1_restore_bottom_only](images/AB-1_restore_bottom_only.png)

### 恢复底部8行像素，再去右侧空白

输入尺寸：[481, 48]；模型输出：`MDIWGQGTMVTVSS`。

![AB-1_restore_bottom_trim_right](images/AB-1_restore_bottom_trim_right.png)

### 恢复底部、去右侧空白，再四周补8像素白边

输入尺寸：[497, 64]；模型输出：`MDIWGQGTMVTVSS`。

![AB-1_restore_bottom_trim_right_pad8](images/AB-1_restore_bottom_trim_right_pad8.png)
