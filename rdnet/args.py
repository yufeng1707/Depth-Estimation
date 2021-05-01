import configparser

from nystrom_attention import Nystromer


class Arg_train:
    def __init__(self):
        configs = configparser.ConfigParser()
        configs.read('train_arg.txt')
        config = configs['COLAB']
        # drive/MyDrive/dataset
        self.data_path = config['data_path']
        self.image_height = int(config['image_height'])  # 480
        self.image_width = int(config['image_width'])  # 640
        self.image_size = []
        self.image_size.append(self.image_height)
        self.image_size.append(self.image_width)
        self.patch_size = int(config['patch_size'])  # 32
        self.knowledge_dims = list(
            map(int, config['knowledge_dims'].split(',')))  # 4096, 2048, 1024
        self.dense_dims = list(
            map(int, config['dense_dims'].split(',')))  # 1024, 1024, 1024, 1024
        self.latent_dims = int(config['latent_dims'])  # 256
        self.emb_size = int(config['emb_size'])  # 4096
        self.use_readout = config['use_readout']  # ignore
        self.hooks = list(
            map(int, config['hooks'].split(',')))  # 3, 6, 9, 12
        self.batch_size = int(config['batch_size'])  # 4
        self.num_epochs = int(config['num_epochs'])  # 50
        self.learning_rate = float(config['learning_rate'])  # 1e-4
        self.weight_decay = float(config['weight_decay'])  # 1e-2
        self.adam_eps = float(config['adam_eps'])  # 1e-3
        self.num_threads = int(config['num_threads'])  # 1
        self.data_path_eval = ''
        self.mode = 'train'
        self.checkpoint_path = config['checkpoint_path']
        self.landmarks = int(config['landmarks'])  # 512
        self.retrain = True
        self.end_learning_rate = -1
        self.variance_focus = float(
            config['variance_focus'])  # 0.85
        self.model_name = 'RDNet'
        self.gpu = 0
        self.log_directory = config['log_directory']
        self.do_online_eval = True
        self.transformer = Nystromer
        self.multiprocessing_distributed = False
        self.log_freq = int(config['log_freq'])  # 100
        self.save_freq = int(config['save_freq'])  # 500
        self.eval_summary_directory = ''
        self.min_depth_eval = float(config['min_depth_eval'])  # 1e-3
        self.max_depth_eval = float(config['max_depth_eval'])  # 80
        self.eval_freq = int(config['eval_freq'])  # 500