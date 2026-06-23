from market_map import generate_market_map_post, post_to_x


def main():
    post = generate_market_map_post()
    if post["image_path"]:
        post_to_x(post["caption"], post["image_path"])
        print(f"ヒートマップ投稿完了: {post['headline']}")
    else:
        # 仕様11: 画像生成失敗 → テキストのみで投稿
        post_to_x(post["caption"])
        print("画像なしでテキスト投稿しました")


if __name__ == "__main__":
    main()
