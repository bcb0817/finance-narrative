<?php
/**
 * Plugin Name: Finance X Radar
 * Description: Displays bounded X Search observations as a market radar.
 * Version: 1.6.2
 * Requires at least: 6.2
 * Requires PHP: 7.4
 * Text Domain: finance-x-radar
 */
defined('ABSPATH') || exit;

final class Finance_X_Radar {
    // WordPress post_type keys are limited to 20 characters.
    const CPT = 'finance_radar_snap';
    const META = '_finance_radar_payload';
    const SCHEMA_VERSION = 1;
    const MAX_BYTES = 131072;

    public static function init() {
        add_action('init', [__CLASS__, 'register_cpt']);
        add_action('rest_api_init', [__CLASS__, 'register_routes']);
        add_shortcode('finance_x_radar', [__CLASS__, 'shortcode']);
        add_action('wp_enqueue_scripts', [__CLASS__, 'assets']);
        add_action('admin_menu', [__CLASS__, 'admin_menu']);
        add_action('wp_head', [__CLASS__, 'language_links']);
        add_filter('the_content', [__CLASS__, 'structure_article'], 15);
        add_filter('the_content', [__CLASS__, 'social_share_bar'], 20);
        add_action('wp_ajax_fxr_count_view', [__CLASS__, 'count_view']);
        add_action('wp_ajax_nopriv_fxr_count_view', [__CLASS__, 'count_view']);
        add_action('finance_x_radar_cleanup', [__CLASS__, 'cleanup']);
        add_filter('wp_robots', [__CLASS__, 'robots']);
        register_activation_hook(__FILE__, [__CLASS__, 'activate']);
        register_deactivation_hook(__FILE__, [__CLASS__, 'deactivate']);
    }

    public static function register_cpt() {
        register_post_type(self::CPT, [
            'labels' => ['name' => 'X Radar Snapshots', 'singular_name' => 'X Radar Snapshot'],
            'public' => false, 'show_ui' => true, 'show_in_rest' => false,
            'supports' => ['title'], 'capability_type' => 'post', 'map_meta_cap' => true,
            'menu_icon' => 'dashicons-chart-area',
        ]);
    }

    public static function activate() {
        self::register_cpt(); flush_rewrite_rules(false);
        if (!wp_next_scheduled('finance_x_radar_cleanup')) wp_schedule_event(time() + HOUR_IN_SECONDS, 'daily', 'finance_x_radar_cleanup');
    }
    public static function deactivate() {
        wp_clear_scheduled_hook('finance_x_radar_cleanup'); flush_rewrite_rules(false);
    }

    public static function register_routes() {
        register_rest_route('finance-radar/v1', '/snapshots', [
            ['methods' => WP_REST_Server::CREATABLE, 'callback' => [__CLASS__, 'create'], 'permission_callback' => [__CLASS__, 'can_write']],
            ['methods' => WP_REST_Server::READABLE, 'callback' => [__CLASS__, 'archive'], 'permission_callback' => '__return_true'],
        ]);
        register_rest_route('finance-radar/v1', '/snapshots/latest', [
            'methods' => WP_REST_Server::READABLE, 'callback' => [__CLASS__, 'latest'], 'permission_callback' => '__return_true',
        ]);
    }

    public static function can_write() { return is_user_logged_in() && current_user_can('publish_posts'); }
    private static function err($code, $message, $status = 400) { return new WP_Error($code, $message, ['status' => $status]); }
    private static function text($value, $max) { return mb_substr(trim(wp_strip_all_tags((string)$value, true)), 0, $max); }
    private static function timestamp($value) { return is_string($value) && strtotime($value) !== false ? $value : ''; }
    private static function x_url($value) {
        $url = esc_url_raw((string)$value, ['https']); if (!$url) return '';
        $parts = wp_parse_url($url); $host = strtolower($parts['host'] ?? ''); $path = $parts['path'] ?? '';
        return in_array($host, ['x.com','www.x.com','twitter.com','www.twitter.com'], true) && preg_match('#^/[A-Za-z0-9_]{1,15}/status/[0-9]+/?$#', $path) ? $url : '';
    }

    public static function validate_payload($data) {
        if (!is_array($data) || strlen(wp_json_encode($data)) > self::MAX_BYTES) return self::err('invalid_size', 'Payload is invalid or too large.', 413);
        $required = ['schema_version','snapshot_id','run_id','observed_at','generated_at','radar_type','disclaimer','topics'];
        foreach ($required as $key) if (!array_key_exists($key, $data)) return self::err('missing_field', 'Missing required field: '.$key);
        if ((int)$data['schema_version'] !== self::SCHEMA_VERSION) return self::err('schema_version', 'Unsupported schema version.');
        if (!is_array($data['topics']) || count($data['topics']) > 5) return self::err('topic_count', 'At most five topics are allowed.');
        if (!self::timestamp($data['observed_at']) || !self::timestamp($data['generated_at'])) return self::err('timestamp', 'Invalid timestamp.');
        foreach ($data['topics'] as $topic) {
            if (!is_array($topic) || empty($topic['topic_id']) || empty($topic['title'])) return self::err('topic_schema', 'Invalid topic.');
            if (!isset($topic['representative_posts']) || !is_array($topic['representative_posts']) || count($topic['representative_posts']) > 2) return self::err('post_count', 'At most two representative posts are allowed.');
            foreach ($topic['representative_posts'] as $post) if (empty(self::x_url($post['url'] ?? ''))) return self::err('post_url', 'Invalid representative post URL.');
        }
        return true;
    }

    public static function sanitize_payload($data) {
        $safe = ['schema_version'=>1, 'snapshot_id'=>self::text($data['snapshot_id'],64), 'run_id'=>self::text($data['run_id'],64),
            'observed_at'=>self::timestamp($data['observed_at']), 'generated_at'=>self::timestamp($data['generated_at']),
            'radar_type'=>self::text($data['radar_type'],40), 'disclaimer'=>self::text($data['disclaimer'],400), 'topics'=>[]];
        foreach (array_slice($data['topics'], 0, 5) as $topic) {
            $row = [
                'topic_id'=>self::text($topic['topic_id'] ?? '',64), 'rank'=>max(1,min(5,(int)($topic['rank'] ?? 1))),
                'title'=>self::text($topic['title'] ?? '',120), 'canonical_topic'=>self::text($topic['canonical_topic'] ?? '',160),
                'tickers'=>array_map(fn($x)=>self::text($x,16),array_slice((array)($topic['tickers'] ?? []),0,10)),
                'entities'=>array_map(fn($x)=>self::text($x,80),array_slice((array)($topic['entities'] ?? []),0,10)),
                'category'=>self::text($topic['category'] ?? 'OTHER',40), 'summary'=>self::text($topic['summary'] ?? '',280),
                'observed_velocity_score'=>max(0,min(100,(float)($topic['observed_velocity_score'] ?? 0))),
                'observed_acceleration_score'=>max(0,min(100,(float)($topic['observed_acceleration_score'] ?? 0))),
                'observed_radar_score'=>max(0,min(100,(float)($topic['observed_radar_score'] ?? 0))),
                'trend_direction'=>in_array($topic['trend_direction'] ?? '', ['up','flat','down'],true)?$topic['trend_direction']:'flat',
                'attention_level'=>in_array($topic['attention_level'] ?? '', ['LOW','NORMAL','HIGH','VERY_HIGH'],true)?$topic['attention_level']:'LOW',
                'reliability'=>in_array($topic['reliability'] ?? '', ['high','medium','low','unknown'],true)?$topic['reliability']:'unknown',
                'official_source_present'=>!empty($topic['official_source_present']), 'source_confirmation_status'=>self::text($topic['source_confirmation_status'] ?? '',80),
                'dominant_narrative'=>self::text($topic['dominant_narrative'] ?? '',280), 'dissent'=>self::text($topic['dissent'] ?? '',280),
                'misconception'=>self::text($topic['misconception'] ?? '',280), 'updated_at'=>self::timestamp($topic['updated_at'] ?? ''), 'representative_posts'=>[]];
            $row['translations'] = [];
            foreach (['ja','en'] as $lang) { $tr=(array)($topic['translations'][$lang] ?? []); $row['translations'][$lang]=[
                'title'=>self::text($tr['title'] ?? $row['title'],120), 'summary'=>self::text($tr['summary'] ?? $row['summary'],280),
                'dominant_narrative'=>self::text($tr['dominant_narrative'] ?? $row['dominant_narrative'],280),
                'dissent'=>self::text($tr['dissent'] ?? $row['dissent'],280), 'misconception'=>self::text($tr['misconception'] ?? $row['misconception'],280)]; }
            foreach (array_slice($topic['representative_posts'],0,2) as $post) $row['representative_posts'][] = [
                'post_id'=>self::text($post['post_id'] ?? '',32), 'handle'=>ltrim(self::text($post['handle'] ?? '',32),'@'),
                'account_type'=>self::text($post['account_type'] ?? 'unknown',40), 'url'=>self::x_url($post['url'] ?? ''),
                'short_summary'=>self::text($post['short_summary'] ?? 'この論点について言及',100), 'why_selected'=>self::text($post['why_selected'] ?? '',160)];
            $safe['topics'][] = $row;
        }
        return $safe;
    }

    public static function create(WP_REST_Request $request) {
        $raw = $request->get_body(); if (strlen($raw) > self::MAX_BYTES) return self::err('invalid_size','Payload too large.',413);
        $data = $request->get_json_params(); $valid = self::validate_payload($data); if (is_wp_error($valid)) return $valid;
        $safe = self::sanitize_payload($data); $existing = get_posts(['post_type'=>self::CPT,'post_status'=>'private','meta_key'=>'_finance_radar_snapshot_id','meta_value'=>$safe['snapshot_id'],'numberposts'=>1]);
        $postarr = ['post_type'=>self::CPT,'post_status'=>'private','post_title'=>'X Radar '.$safe['observed_at']];
        if ($existing) $postarr['ID'] = $existing[0]->ID;
        $id = wp_insert_post($postarr, true); if (is_wp_error($id)) return $id;
        update_post_meta($id, '_finance_radar_snapshot_id', $safe['snapshot_id']); update_post_meta($id, self::META, wp_json_encode($safe, JSON_UNESCAPED_UNICODE));
        return new WP_REST_Response(['status'=>$existing?'updated':'created','snapshot_id'=>$safe['snapshot_id'],'topic_count'=>count($safe['topics']),'url'=>home_url('/?page_id=10')], $existing?200:201);
    }

    private static function query($limit=1) { return get_posts(['post_type'=>self::CPT,'post_status'=>'private','numberposts'=>$limit,'orderby'=>'date','order'=>'DESC']); }
    private static function payload($post) { $data=json_decode(get_post_meta($post->ID,self::META,true),true); return is_array($data)?$data:null; }
    public static function latest() { $posts=self::query(1); return $posts ? rest_ensure_response(self::payload($posts[0])) : self::err('not_found','No snapshot.',404); }
    public static function archive(WP_REST_Request $request) { $limit=max(1,min(30,(int)$request->get_param('limit')?:10)); return rest_ensure_response(array_values(array_filter(array_map([__CLASS__,'payload'],self::query($limit))))); }

    public static function assets() {
        if (is_singular() && has_shortcode(get_post_field('post_content',get_queried_object_id()),'finance_x_radar')) wp_enqueue_style('finance-x-radar',plugins_url('assets/radar.css',__FILE__),[], '1.6.2');
        if (is_singular('post')) {
            wp_enqueue_style('finance-x-radar-article', plugins_url('assets/article.css',__FILE__),[], '1.6.2');
            wp_enqueue_style('finance-x-radar-share', plugins_url('assets/share.css',__FILE__),[], '1.6.2');
            wp_enqueue_script('finance-x-radar-share', plugins_url('assets/share.js',__FILE__),[], '1.6.2', true);
            wp_localize_script('finance-x-radar-share', 'fxrArticle', ['ajaxUrl'=>admin_url('admin-ajax.php'),'postId'=>get_queried_object_id()]);
        }
    }

    public static function count_view() {
        $post_id=absint($_POST['post_id']??0); $post=get_post($post_id);
        if (!$post || $post->post_type!=='post' || $post->post_status!=='publish') wp_send_json_error(['message'=>'Invalid post.'],400);
        $ip=(string)($_SERVER['REMOTE_ADDR']??''); $agent=(string)($_SERVER['HTTP_USER_AGENT']??'');
        $fingerprint=hash_hmac('sha256',$post_id.'|'.$ip.'|'.$agent,wp_salt('nonce')); $lock='fxr_view_'.substr($fingerprint,0,32);
        if (get_transient($lock)===false) {
            $views=max(0,(int)get_post_meta($post_id,'_fxr_view_count',true))+1;
            update_post_meta($post_id,'_fxr_view_count',$views); set_transient($lock,1,30*MINUTE_IN_SECONDS);
        } else $views=max(0,(int)get_post_meta($post_id,'_fxr_view_count',true));
        wp_send_json_success(['views'=>$views]);
    }

    public static function structure_article($content) {
        if (is_admin() || is_feed() || !is_singular('post') || !in_the_loop() || !is_main_query()) return $content;
        $author_links='<p class="finance-author-socials"><a href="https://x.com/singa9999" target="_blank" rel="noopener noreferrer me">X（@singa9999）</a><span aria-hidden="true">｜</span><a href="https://www.threads.net/@singa9999" target="_blank" rel="noopener noreferrer me">Threads（@singa9999）</a></p>';
        if (strpos($content,'finance-author-socials')===false) {
            if (strpos($content,'finance-author-profile')!==false) $content=preg_replace('/(<div[^>]*finance-author-profile[^>]*>.*?)(<\/div>)/is','$1'.$author_links.'$2',$content,1);
            else $content.='<aside class="finance-author-profile"><p><strong>執筆者：アルゴ猫ミオ｜米国株の売買シグナル</strong></p>'.$author_links.'<p>市場データとX Searchの限定的な観測をもとに、米国株・日本株の値動きを図解で分析します。</p></aside>';
        }
        $items=[]; $index=0;
        $content=preg_replace_callback('/<h2([^>]*)>(.*?)<\/h2>/is',function($match)use(&$items,&$index){
            $index++; $label=trim(wp_strip_all_tags($match[2])); if ($label==='') return $match[0];
            $id='article-section-'.$index; $attrs=$match[1];
            if (preg_match('/\sid=["\'][^"\']+["\']/', $attrs, $found)) $id=trim(substr($found[0],strpos($found[0],'=')+1)," \t\n\r\0\x0B\"'");
            else $attrs.=' id="'.esc_attr($id).'"';
            $items[]=['id'=>$id,'label'=>$label]; return '<h2'.$attrs.'><span class="fxr-section-number" aria-hidden="true">'.sprintf('%02d',$index).'</span>'.$match[2].'</h2>';
        },$content);
        if (count($items)<2) return $content;
        $label=strpos(determine_locale(),'ja')===0?'この記事のポイント':'In this article'; $toc='<nav class="fxr-article-toc" aria-label="'.esc_attr($label).'"><p>'.esc_html($label).'</p><ol>';
        foreach($items as $item) $toc.='<li><a href="#'.esc_attr($item['id']).'">'.esc_html($item['label']).'</a></li>';
        $toc.='</ol></nav>'; return preg_replace('/<\/p>/i','</p>'.$toc,$content,1);
    }

    public static function social_share_bar($content) {
        if (is_admin() || is_feed() || !is_singular('post') || !in_the_loop() || !is_main_query()) return $content;
        $url=get_permalink(); $title=wp_strip_all_tags(get_the_title()); $encoded_url=rawurlencode($url); $encoded_title=rawurlencode($title);
        $is_ja=strpos(determine_locale(),'ja')===0; $heading=$is_ja?'この記事をシェア':'Share this article'; $copy=$is_ja?'リンクをコピー':'Copy link'; $views=max(0,(int)get_post_meta(get_the_ID(),'_fxr_view_count',true));
        $links=[
            ['X','https://twitter.com/intent/tweet?text='.$encoded_title.'&url='.$encoded_url.'&via=singa9999','x'],
            ['Facebook','https://www.facebook.com/sharer/sharer.php?u='.$encoded_url,'facebook'],
            ['Threads','https://www.threads.net/intent/post?text='.$encoded_title.'%20'.$encoded_url,'threads'],
            ['LinkedIn','https://www.linkedin.com/sharing/share-offsite/?url='.$encoded_url,'linkedin'],
            ['LINE','https://social-plugins.line.me/lineit/share?url='.$encoded_url.'&text='.$encoded_title,'line'],
            [$is_ja?'はてな':'Hatena','https://b.hatena.ne.jp/add?mode=confirm&url='.$encoded_url.'&title='.$encoded_title,'hatena'],
        ];
        ob_start(); ?><aside class="fxr-share" aria-label="<?php echo esc_attr($heading); ?>"><div class="fxr-share__top"><p class="fxr-share__heading"><?php echo esc_html($heading); ?></p><p class="fxr-share__views" aria-live="polite"><span aria-hidden="true">◉</span> <?php echo esc_html($is_ja?'閲覧数':'Views'); ?> <strong data-fxr-view-count><?php echo esc_html(number_format_i18n($views)); ?></strong></p></div><div class="fxr-share__buttons"><?php foreach($links as $link): ?><a class="fxr-share__button fxr-share__button--<?php echo esc_attr($link[2]); ?>" href="<?php echo esc_url($link[1]); ?>" target="_blank" rel="noopener noreferrer nofollow" aria-label="<?php echo esc_attr($link[0].'でシェア'); ?>"><?php echo esc_html($link[0]); ?></a><?php endforeach; ?><button type="button" class="fxr-share__button fxr-share__button--copy" data-fxr-copy-url="<?php echo esc_url($url); ?>" data-copy-label="<?php echo esc_attr($copy); ?>" data-copied-label="<?php echo esc_attr($is_ja?'コピーしました':'Copied'); ?>"><?php echo esc_html($copy); ?></button></div></aside><?php
        return $content.ob_get_clean();
    }
    public static function shortcode($atts) {
        $atts=shortcode_atts(['lang'=>'ja'],$atts,'finance_x_radar'); $lang=$atts['lang']==='en'?'en':'ja';
        $posts=self::query(1); if (!$posts) return '<div class="fxr-empty">まだ観測データがありません。</div>';
        $data=self::payload($posts[0]); $filter=isset($_GET['fxr_category'])?self::text(wp_unslash($_GET['fxr_category']),40):'ALL';
        $categories=['ALL','US STOCKS','JAPAN STOCKS','AI / SEMICONDUCTOR','FX','MACRO'];
        $labels=$lang==='en'?['heading'=>'Financial themes observed through X Search','updated'=>'Last updated','score'=>'Observed radar score','velocity'=>'Observed velocity','acceleration'=>'Observed acceleration','reliability'=>'Reliability','trend'=>'Observed trend','main'=>'Current main discussion','dissent'=>'Different view','misconception'=>'Common misconception','official'=>'Official source','posts'=>'Related posts','view'=>'View on X','disclaimer'=>'This page analyzes a limited set of X Search results. It does not represent total activity on X or the views of the overall market.']:['heading'=>'X Searchで観測された金融テーマ','updated'=>'最終更新','score'=>'観測レーダースコア','velocity'=>'観測velocity','acceleration'=>'観測acceleration','reliability'=>'信頼性','trend'=>'観測トレンド','main'=>'現在の主な論点','dissent'=>'異なる見方','misconception'=>'誤解されやすい点','official'=>'公式ソース','posts'=>'関連投稿','view'=>'Xで投稿を見る','disclaimer'=>$data['disclaimer']];
        $jst = new DateTimeZone('Asia/Tokyo'); ob_start(); ?><section class="fxr" lang="<?php echo esc_attr($lang); ?>" aria-label="X Market Radar"><header><p class="fxr-kicker">X MARKET RADAR</p><h2><?php echo esc_html($labels['heading']); ?></h2><p><?php echo esc_html($labels['updated']); ?>：<?php echo esc_html(wp_date('Y/m/d H:i',strtotime($data['observed_at']),$jst)); ?> JST</p><p class="fxr-disclaimer"><?php echo esc_html($labels['disclaimer']); ?></p></header>
        <nav class="fxr-filters" aria-label="カテゴリ"><?php foreach($categories as $cat): ?><a class="<?php echo $filter===$cat?'is-active':''; ?>" href="<?php echo esc_url(add_query_arg('fxr_category',$cat)); ?>"><?php echo esc_html($cat); ?></a><?php endforeach; ?></nav>
        <div class="fxr-grid"><?php foreach($data['topics'] as $topic): if($filter!=='ALL' && $topic['category']!==$filter) continue; $arrow=['up'=>'↑','flat'=>'→','down'=>'↓'][$topic['trend_direction']]??'→'; ?>
        <?php $copy=(array)($topic['translations'][$lang]??$topic); $score=(float)($topic['observed_radar_score']??0); ?>
        <article class="fxr-card"><div class="fxr-rank"><?php echo (int)$topic['rank']; ?></div><div><div class="fxr-title-row"><h3><?php echo esc_html($copy['title']??$topic['title']??''); ?></h3><span class="fxr-attention <?php echo esc_attr(strtolower($topic['attention_level']??'LOW')); ?>"><?php echo esc_html(str_replace('_',' ',$topic['attention_level']??'LOW')); ?></span></div><p class="fxr-tickers"><?php echo esc_html(implode(' / ',(array)($topic['tickers']??[]))); ?></p>
        <div class="fxr-visual"><div class="fxr-gauge" style="--fxr-score:<?php echo esc_attr($score); ?>"><div><strong><?php echo esc_html(number_format_i18n($score,1)); ?></strong><span>/100</span></div></div><div class="fxr-metrics"><div><label><?php echo esc_html($labels['velocity']); ?></label><span><i style="width:<?php echo esc_attr((float)($topic['observed_velocity_score']??0)); ?>%"></i></span><b><?php echo esc_html(number_format_i18n((float)($topic['observed_velocity_score']??0),1)); ?></b></div><div><label><?php echo esc_html($labels['acceleration']); ?></label><span><i style="width:<?php echo esc_attr((float)($topic['observed_acceleration_score']??0)); ?>%"></i></span><b><?php echo esc_html(number_format_i18n((float)($topic['observed_acceleration_score']??0),1)); ?></b></div><p><?php echo esc_html($labels['trend']); ?> <strong><?php echo esc_html($arrow); ?></strong>　<?php echo esc_html($labels['reliability'].' '.strtoupper($topic['reliability']??'unknown')); ?></p></div></div>
        <p class="fxr-summary"><?php echo esc_html($copy['summary']??$topic['summary']??''); ?></p>
        <div class="fxr-flow"><?php if($copy['dominant_narrative']): ?><section class="fxr-flow-main"><h4><?php echo esc_html($labels['main']); ?></h4><p><?php echo esc_html($copy['dominant_narrative']); ?></p></section><?php endif; if($copy['dissent']): ?><section class="fxr-flow-alt"><h4><?php echo esc_html($labels['dissent']); ?></h4><p><?php echo esc_html($copy['dissent']); ?></p></section><?php endif; if($copy['misconception']): ?><section class="fxr-flow-risk"><h4><?php echo esc_html($labels['misconception']); ?></h4><p><?php echo esc_html($copy['misconception']); ?></p></section><?php endif; ?></div>
        <p class="fxr-source"><?php echo esc_html($labels['official']); ?>：<?php echo $topic['official_source_present']?($lang==='en'?'Observed':'観測あり'):($lang==='en'?'Unconfirmed':'未確認'); ?> / <?php echo esc_html($topic['source_confirmation_status']); ?></p>
        <?php if($topic['representative_posts']): ?><h4><?php echo esc_html($labels['posts']); ?></h4><ul class="fxr-posts"><?php foreach($topic['representative_posts'] as $post): ?><li><span>@<?php echo esc_html($post['handle']); ?></span><a href="<?php echo esc_url($post['url']); ?>" target="_blank" rel="noopener noreferrer nofollow"><?php echo esc_html($labels['view']); ?></a></li><?php endforeach; ?></ul><?php endif; ?><time><?php echo esc_html(wp_date('Y/m/d H:i',strtotime($topic['updated_at']),$jst)); ?> JST</time></div></article><?php endforeach; ?></div></section><?php return ob_get_clean();
    }

    private static function retention_days() { $configured=defined('FINANCE_RADAR_RETENTION_DAYS')?FINANCE_RADAR_RETENTION_DAYS:getenv('FINANCE_RADAR_RETENTION_DAYS'); return max(1,(int)($configured?:30)); }
    public static function cleanup() { $days=self::retention_days(); $old=get_posts(['post_type'=>self::CPT,'post_status'=>'private','numberposts'=>100,'date_query'=>[['before'=>$days.' days ago']],'fields'=>'ids']); foreach($old as $id) wp_delete_post($id,true); }
    public static function robots($robots) { if (is_singular(self::CPT) || is_post_type_archive(self::CPT)) { $robots['noindex']=true; $robots['nofollow']=true; } return $robots; }
    public static function language_links() { if (!is_page(['x-market-radar-ja','x-market-radar-en'])) return; $ja=get_page_by_path('x-market-radar-ja'); $en=get_page_by_path('x-market-radar-en'); if($ja&&$en){ echo '<link rel="alternate" hreflang="ja" href="'.esc_url(get_permalink($ja)).'" />\n'; echo '<link rel="alternate" hreflang="en" href="'.esc_url(get_permalink($en)).'" />\n'; echo '<link rel="alternate" hreflang="x-default" href="'.esc_url(get_permalink($ja)).'" />\n'; } }
    public static function admin_menu() { add_submenu_page('edit.php?post_type='.self::CPT,'X Radar Status','Status','manage_options','finance-x-radar-status',[__CLASS__,'admin_page']); }
    public static function admin_page() { $posts=self::query(1); echo '<div class="wrap"><h1>Finance X Radar Status</h1><p>REST endpoint: <code>'.esc_html(rest_url('finance-radar/v1/snapshots')).'</code></p><p>Latest snapshot: '.($posts?esc_html($posts[0]->post_title):'none').'</p><p>Retention: '.self::retention_days().' days</p></div>'; }
}
Finance_X_Radar::init();
