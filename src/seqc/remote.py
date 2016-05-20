import time
import sys
import os
import configparser
import random
from subprocess import Popen, PIPE
import shutil
import paramiko
import boto3
from botocore.exceptions import ClientError
import seqc
import logging
# todo: delete this after testing
from subprocess import check_output

# turn off paramiko non-error logging
logging.getLogger('paramiko').setLevel(logging.CRITICAL)


class EC2RuntimeError(Exception):
    pass


class VolumeCreationError(Exception):
    pass


class SpotBidError(Exception):
    pass


class ClusterServer(object):
    """Connects to AWS instance using paramiko and a private RSA key,
    allows for the creation/manipulation of EC2 instances and executions
    of commands on the remote server"""

    def __init__(self):

        self.keyname = None
        self.keypath = None
        self.image_id = None
        self.inst_type = None
        self.subnet = None
        self.zone = None
        self.ec2 = boto3.resource('ec2')
        self.inst_id = None
        self.sg = None
        self.serv = None
        self.aws_id = None
        self.aws_key = None
        self.spot_bid = None

    def create_security_group(self):
        """Creates a new security group for the cluster
        :param name: cluster name if provided by user
        """
        name = 'SEQC-%07d' % random.randint(1, int(1e7))
        seqc.log.notify('Assigned instance name %s.' % name)
        try:
            sg = self.ec2.create_security_group(GroupName=name, Description=name)
            sg.authorize_ingress(IpProtocol="tcp", CidrIp="0.0.0.0/0", FromPort=22,
                                 ToPort=22)
            sg.authorize_ingress(SourceSecurityGroupName=name)
            self.sg = sg.id

            seqc.log.notify('Created security group %s (%s).' % (name, sg.id))
        except ClientError:
            seqc.log.notify('Instance %s already exists! Exiting.' % name)
            sys.exit(2)

    def configure_cluster(self, config_file):
        """configures the newly created cluster according to config
        :param config_file: /path/to/seqc/config
        """
        config = configparser.ConfigParser()
        config.read(config_file)
        template = config['global']['default_template']
        self.keyname = config['key']['rsa_key_name']
        self.keypath = os.path.expanduser(config['key']['rsa_key_location'])
        self.image_id = config[template]['node_image_id']
        self.inst_type = config[template]['node_instance_type']
        self.subnet = config['c4']['subnet_id']
        self.zone = config[template]['availability_zone']
        self.aws_id = config['aws_info']['aws_access_key_id']
        self.aws_key = config['aws_info']['aws_secret_access_key']
        self.spot_bid = config['SpotBid']['spot_bid']

    def create_spot_cluster(self, volume_size):
        """launches an instance using the specified spot bid"""

        client = boto3.client('ec2')
        if 'c4' in self.inst_type:
            if not self.subnet:
                raise ValueError('A subnet-id must be specified for C4 instances!')
            client.request_spot_instances(
                DryRun=False,
                SpotPrice=self.spot_bid,
                LaunchSpecification={
                    'ImageId': self.image_id,
                    'KeyName': self.keyname,
                    'InstanceType': self.inst_type,
                    'Placement': {
                        'AvailabilityZone': self.zone
                    },
                    'BlockDeviceMappings': [
                        {
                            'DeviceName': '/dev/xvdf',
                            'Ebs': {
                                'VolumeSize': volume_size,
                                'DeleteOnTermination': True,
                            }
                        }
                    ],
                    'SubnetId': self.subnet,
                    'SecurityGroupIds': [self.sg],
                }
            )

        elif 'c3' in self.inst_type:
            client.request_spot_instances(
                DryRun=False,
                SpotPrice=self.spot_bid,
                LaunchSpecification={
                    'ImageId': self.image_id,
                    'KeyName': self.keyname,
                    'InstanceType': self.inst_type,
                    'Placement': {
                        'AvailabilityZone': self.zone
                    },
                    'BlockDeviceMappings': [
                        {
                            'DeviceName': '/dev/xvdf',
                            'Ebs': {
                                'VolumeSize': volume_size,
                                'DeleteOnTermination': True,
                            }
                        }
                    ],
                    'SecurityGroupIds': [self.sg],
                }
            )

        # check status of spot bid request
        ids = []
        all_resp = client.describe_spot_instance_requests()['SpotInstanceRequests']
        for item in all_resp:
            try:
                ids.append(item['InstanceId'])
            except KeyError:
                continue
        idx = ids.index(self.inst_id.id)
        spot_resp = all_resp[idx]
        max_tries = 40
        i = 0
        seqc.log.info('Waiting for spot bid request to be fulfilled...')
        while spot_resp['State'] != 'active':
            status_code = spot_resp['Status']['Code']
            bad_status = ['price-too-low', 'capacity-oversubscribed',
                          'capacity-not-available', 'launch-group-constraint',
                          'az-group-constraint', 'placement-group-constraint',
                          'constraint-not-fulfillable', 'schedule-expired',
                          'bad-parameters', 'system-error']
            if status_code in bad_status:
                raise SpotBidError('Please adjust your spot bid request.')
            time.sleep(10)
            spot_resp = client.describe_spot_instance_requests()[
                'SpotInstanceRequests'][idx]
            i += 1
            if i >= max_tries:
                raise SpotBidError('Timeout: spot bid could not be fulfilled.')
        # spot request was approved, instance launched
        self.inst_id = self.ec2.Instance(spot_resp['InstanceId'])

    def create_cluster(self):
        """creates a new AWS cluster with specifications from config"""

        if 'c4' in self.inst_type:
            if not self.subnet:
                raise ValueError('A subnet-id must be specified for C4 instances!')
            else:
                clust = self.ec2.create_instances(ImageId=self.image_id, MinCount=1,
                                                  MaxCount=1,
                                                  KeyName=self.keyname,
                                                  InstanceType=self.inst_type,
                                                  Placement={
                                                      'AvailabilityZone': self.zone},
                                                  SecurityGroupIds=[self.sg],
                                                  SubnetId=self.subnet)
        elif 'c3' in self.inst_type:
            clust = self.ec2.create_instances(ImageId=self.image_id, MinCount=1,
                                              MaxCount=1,
                                              KeyName=self.keyname,
                                              InstanceType=self.inst_type,
                                              Placement={'AvailabilityZone': self.zone},
                                              SecurityGroupIds=[self.sg])
        else:
            raise ValueError('self.inst_type must be a c3 or c4 instance')
        instance = clust[0]
        seqc.log.notify('Created new instance %s. Waiting until instance is running' %
                        instance)
        instance.wait_until_exists()
        instance.wait_until_running()
        seqc.log.notify('Instance %s now running.' % instance)
        self.inst_id = instance

    def cluster_is_running(self):
        """checks whether a cluster is running"""
        if self.inst_id is None:
            raise EC2RuntimeError('No inst_id assigned. Instance was not successfully '
                                  'created!')
        self.inst_id.reload()
        if self.inst_id.state['Name'] == 'running':
            return True
        else:
            return False

    def restart_cluster(self):
        """restarts a stopped cluster"""
        if self.inst_id.state['Name'] == 'stopped':
            self.inst_id.start()
            self.inst_id.wait_until_running()
            seqc.log.notify('Stopped instance %s has restarted.' % self.inst_id.id)
        else:
            seqc.log.notify('Instance %s is not in a stopped state!' %
                            self.inst_id.id)

    def stop_cluster(self):
        """stops a running cluster"""
        if self.cluster_is_running():
            self.inst_id.stop()
            self.inst_id.wait_until_stopped()
            seqc.log.notify('Instance %s is now stopped.' % self.inst_id)
        else:
            seqc.log.notify('Instance %s is not running!' % self.inst_id)

    def create_volume(self, vol_size):
        """creates a volume of size vol_size and returns the volume's id"""
        vol = self.ec2.create_volume(Size=vol_size, AvailabilityZone=self.zone,
                                     VolumeType='gp2')
        vol_id = vol.id
        vol_state = vol.state
        max_tries = 40
        i = 0
        while vol_state != 'available':
            time.sleep(3)
            vol.reload()
            i += 1
            if i >= max_tries:
                raise VolumeCreationError('Volume could not be created.')
            vol_state = vol.state
        seqc.log.notify('Volume %s created successfully.' % vol_id)
        return vol_id

    def attach_volume(self, vol_id, dev_id):
        """attaches a vol_id to inst_id at dev_id
        :param dev_id: where volume will be mounted
        :param vol_id: ID of volume to be attached
        """
        vol = self.ec2.Volume(vol_id)
        self.inst_id.attach_volume(VolumeId=vol_id, Device=dev_id)
        max_tries = 40
        i = 0
        while vol.state != 'in-use':
            time.sleep(.5)
            vol.reload()
            i += 1
            if i >= max_tries:
                raise VolumeCreationError('Volume could not be attached.')
        resp = self.inst_id.modify_attribute(
            BlockDeviceMappings=[
                {'DeviceName': dev_id, 'Ebs': {'VolumeId': vol.id,
                                               'DeleteOnTermination': True}}])
        if resp['ResponseMetadata']['HTTPStatusCode'] != 200:
            EC2RuntimeError('Something went wrong modifying the attribute of the Volume!')

        # wait until all volumes are attached
        device_info = self.inst_id.block_device_mappings
        for i in range(1, len(device_info)):
            status = device_info[i]['Ebs']['Status']
            i = 0
            while status != 'attached':
                time.sleep(.5)
                self.inst_id.reload()
                device_info = self.inst_id.block_device_mappings
                status = device_info[i]['Ebs']['Status']
                i += 1
                if i >= max_tries:
                    raise VolumeCreationError('All Volumes could not be attached')
        seqc.log.notify('Volume %s attached to %s at %s.' %
                        (vol_id, self.inst_id.id, dev_id))

    def connect_server(self):
        """connects to the aws instance"""
        ssh_server = SSHServer(self.inst_id.id, self.keypath)
        seqc.log.notify('Connecting to instance %s...' % self.inst_id.id)
        ssh_server.connect()
        if ssh_server.is_connected():
            seqc.log.notify('Connection successful!')
        self.serv = ssh_server

    def allocate_space(self, spot: bool, vol_size: int):
        """dynamically allocates the specified amount of space on /data"""

        dev_id = "/dev/xvdf"
        if not spot:
            seqc.log.notify("Creating volume of size %d GB..." % vol_size)
            vol_id = self.create_volume(vol_size)
            self.attach_volume(vol_id, dev_id)
            seqc.log.notify("Successfully attached %d GB in 1 volume." % vol_size)

        self.serv.exec_command("sudo mkfs -t ext4 %s" % dev_id)
        self.serv.exec_command("sudo mkdir -p /data")
        self.serv.exec_command("sudo mount %s /data" % dev_id)
        seqc.log.notify("Successfully mounted new volume onto /data.")

    def git_pull(self):
        """installs the SEQC directory in /data/software"""
        # todo: replace this with git clone once seqc repo is public

        folder = '/data/software/'
        seqc.log.notify('Installing SEQC on remote instance.')
        self.serv.exec_command("sudo mkdir %s" % folder)
        self.serv.exec_command("sudo chown -c ubuntu /data")
        self.serv.exec_command("sudo chown -c ubuntu %s" % folder)

        location = folder + 'seqc.tar.gz'
        # self.serv.exec_command(
        #     'curl -H "Authorization: token a22b2dc21f902a9a97883bcd136d9e1047d6d076" -L '
        #     'https://api.github.com/repos/ambrosejcarr/seqc/tarball/{version} | '
        #     'sudo tee {location} > /dev/null'.format(
        #         location=location, version=seqc.__version__))
        self.serv.exec_command(
            'curl -H "Authorization: token a22b2dc21f902a9a97883bcd136d9e1047d6d076" -L '
            'https://api.github.com/repos/ambrosejcarr/seqc/tarball/{version} | '
            'sudo tee {location} > /dev/null'.format(
                location=location, version='vol_update'))
        self.serv.exec_command('cd %s; mkdir seqc && tar -xvf seqc.tar.gz -C seqc '
                               '--strip-components 1' % folder)
        self.serv.exec_command('cd %s; sudo pip3 install -e ./' % folder + 'seqc')
        num_retries = 30
        install_fail = True
        for i in range(num_retries):
            out, err = self.serv.exec_command('process_experiment.py -h | grep RNA')
            if not out:
                time.sleep(2)
            else:
                install_fail = False
                break
        if not install_fail:
            seqc.log.notify('SEQC successfully installed in %s.' % folder)
        else:
            raise EC2RuntimeError('Error installing SEQC on the cluster.')

    def set_credentials(self):
        self.serv.exec_command('aws configure set aws_access_key_id %s' % self.aws_id)
        self.serv.exec_command(
            'aws configure set aws_secret_access_key %s' % self.aws_key)
        self.serv.exec_command('aws configure set region %s' % self.zone[:-1])

    def cluster_setup(self, volsize):
        config_file = os.path.expanduser('~/.seqc/config')
        self.configure_cluster(config_file)
        self.create_security_group()

        # modified cluster creation for spot bid
        if self.spot_bid != 'None':
            self.create_spot_cluster(volsize)
            self.connect_server()
            self.allocate_space(True, volsize)
        else:
            self.create_cluster()
            self.connect_server()
            self.allocate_space(False, volsize)
        self.git_pull()
        self.set_credentials()
        seqc.log.notify('Remote instance successfully configured.')


def terminate_cluster(instance_id):
    """terminates a running cluster
    :param instance_id:
    """
    ec2 = boto3.resource('ec2')
    instance = ec2.Instance(instance_id)

    try:
        if instance.state['Name'] == 'running':
            instance.terminate()
            instance.wait_until_terminated()
            seqc.log.notify('termination complete!')
        else:
            seqc.log.notify('instance %s is not running!' % instance_id)
    except ClientError:
        seqc.log.notify('instance %s does not exist!' % instance_id)


def remove_sg(sg_id):
    ec2 = boto3.resource('ec2')
    sg = ec2.SecurityGroup(sg_id)
    sg_name = sg.group_name
    try:
        sg.delete()
        seqc.log.notify('security group %s (%s) successfully removed' % (
            sg_name, sg_id))
    except ClientError:
        seqc.log.notify('security group %s (%s) is still in use!' % (sg_name, sg_id))


def email_user(attachment: str, email_body: str, email_address: str) -> None:
    """
    sends an email to email address with text contents of email_body and attachment
    attached. Email will come from "Ubuntu@<ec2-instance-ip-of-aws-instance>

    :param attachment: the file location of the attachment to append to the email
    :param email_body: text to send in the body of the email
    :param email_address: the address to which the email should be sent.
    """
    if isinstance(email_body, str):
        email_body = email_body.encode()
    # Note: exceptions used to be logged here, but this is not the right place for it.
    email_args = ['mutt', '-a', attachment, '-s', 'Remote Process', '--', email_address]
    email_process = Popen(email_args, stdin=PIPE)
    email_process.communicate(email_body)


def gzip_file(filename):
    """gzips a given file using pigz, returns name of gzipped file"""
    cmd = 'pigz ' + filename
    pname = Popen(cmd.split())
    pname.communicate()
    return filename + '.gz'


def upload_results(output_stem: str, email_address: str, aws_upload_key: str,
                   start_pos: str) -> None:
    """
    :param output_stem: specified output directory in cluster
    :param email_address: e-mail where run summary will be sent
    :param aws_upload_key: tar gzipped files will be uploaded to this S3 bucket
    :param infile: determines where in the script SEQC started
    """
    prefix, directory = os.path.split(output_stem)
    counts = output_stem + '_read_and_count_matrices.p'
    log = prefix + '/seqc.log'
    files = [counts, log]  # counts and seqc.log will always be uploaded

    # start_pos can be: start, merged, samfile, readarray
    if start_pos == 'start' or start_pos == 'merged':
        # samfile = prefix + '/alignments/Aligned.out.sam'
        # bamfile = prefix + '/alignments/Aligned.out.bam'
        alignment_summary = output_stem + '_alignment_summary.txt'

        # todo: delete this after testing
        output = check_output(['df', '-h']).decode()
        seqc.log.info('Just running df -h to try:')
        seqc.log.info(output)

        # converting samfile to bamfile
        # convert_sam = 'samtools view -bS -o {bamfile} {samfile}'.format(bamfile=bamfile,
        #                                                                 samfile=samfile)
        # conv = Popen(convert_sam.split())
        # conv.communicate()
        # seqc.log.info('Successfully converted samfile to bamfile to upload.')

        shutil.copyfile(prefix + '/alignments/Log.final.out', output_stem +
                        '_alignment_summary.txt')
        files.append(alignment_summary)
        # files.append(bamfile)

        # if start_pos != 'merged':
        #     merged_fastq = output_stem + '_merged.fastq'
        #     # zipping merged_fastq file to upload
        #     merged_fastq = gzip_file(merged_fastq)
        #     seqc.log.info('Successfully gzipped merged fastq file to upload.')
        #     files.append(merged_fastq)

    # if start_pos != 'readarray':
    #     h5_archive = output_stem + '.h5'
    #     files.append(h5_archive)

    bucket, key = seqc.io.S3.split_link(aws_upload_key)
    for item in files:
        try:
            seqc.io.S3.upload_file(item, bucket, key)
            item_name = item.split('/')[-1]
            seqc.log.info('Successfully uploaded %s to the specified S3 location '
                          '"%s%s".' % (item, aws_upload_key, item_name))
        except FileNotFoundError:
            seqc.log.notify('Item %s was not found! Continuing with upload...' % item)

    # todo @AJC put this back in
    # generate a run summary and append to the email
    # exp = seqc.Experiment.from_npz(counts)
    # run_summary = exp.summary(alignment_summary)
    run_summary = ''

    # get the name of the output file
    seqc.log.info('Upload complete. An e-mail will be sent to %s.' % email_address)

    # email results to user
    body = ('SEQC RUN COMPLETE.\n\n'
            'The run log has been attached to this email and '
            'results are now available in the S3 location you specified: '
            '"%s"\n\n'
            'RUN SUMMARY:\n\n%s' % (aws_upload_key, repr(run_summary)))
    email_user(log, body, email_address)
    seqc.log.info('SEQC run complete. Cluster will be terminated unless --no-terminate '
                  'flag was specified.')


def check_progress():
    # reading in configuration file
    config_file = os.path.expanduser('~/.seqc/config')
    config = configparser.ConfigParser()
    if not config.read(config_file):
        raise ValueError('Please run ./configure (found in the seqc directory) before '
                         'attempting to run process_experiment.py.')

    # obtaining rsa key from configuration file
    rsa_key = os.path.expanduser(config['key']['rsa_key_location'])

    # checking for instance status
    inst_file = os.path.expanduser('~/.seqc/instance.txt')
    try:
        with open(inst_file, 'r') as f:
            for line in f:
                entry = line.strip('\n')
                inst_id, run_name = entry.split(':')

                # connecting to the remote instance
                s = seqc.remote.SSHServer(inst_id, rsa_key)
                try:
                    inst_state = s.instance.state['Name']
                    if inst_state != 'running':
                        print('Cluster (%s) for run "%s" is currently %s.' %
                              (inst_id, run_name, inst_state))
                        continue
                except:
                    print('Cluster (%s) for run "%s" has been terminated.'
                          % (inst_id,run_name))
                    continue

                s.connect()
                out, err = s.exec_command('less /data/seqc.log')
                if not out:
                    print('ERROR: SEQC log file not found in cluster (%s) for run "%s." '
                          'Something went wrong during remote run.' % (inst_id, run_name))
                    continue
                print('-'*80)
                print('Printing contents of the remote SEQC log file for run "%s":' % run_name)
                print('-'*80)
                for x in out:
                    print(x)
                print('-'*80 + '\n')
    except FileNotFoundError:
        print('You have not started a remote instance -- exiting.')
        sys.exit(0)


class SSHServer(object):
    def __init__(self, inst_id, keypath):
        ec2 = boto3.resource('ec2')
        self.instance = ec2.Instance(inst_id)

        if not os.path.isfile(keypath):
            raise ValueError('ssh key not found at provided keypath: %s' % keypath)
        self.key = keypath
        self.ssh = paramiko.SSHClient()

    def connect(self):
        max_attempts = 25
        attempt = 1
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        dns = self.instance.public_dns_name
        while True:
            try:
                self.ssh.connect(dns, username='ubuntu', key_filename=self.key)
                break
            # except paramiko.AuthenticationException:
            #     print('autherror')
            #     print('instance not ready for connection, sleeping...')
            #     self.instance.reload()
            #     time.sleep(30)
            # except paramiko.SSHException:
            #     print('ssherror')
            #     print('instance not ready for connection, sleeping...')
            #     self.instance.reload()
            #     time.sleep(30)
            except FileNotFoundError:
                seqc.log.notify('The key %s was not found!' % self.key)
                sys.exit(2)
            # except paramiko.BadHostKeyException:
            #     print('the host key %s could not be verified!' %self.key)
            #     sys.exit(2)
            except:
                seqc.log.notify('Not yet connected, sleeping (try %d of %d)' % (
                    attempt, max_attempts))
                time.sleep(4)
                attempt += 1
                if attempt > max_attempts:
                    raise

    def is_connected(self):
        if self.ssh.get_transport() is None:
            return False
        else:
            return True

    def disconnect(self):
        if self.is_connected():
            self.ssh.close()

    def get_file(self, localfile, remotefile):
        if not self.is_connected():
            seqc.log.notify('You are not connected!')
            sys.exit(2)
        ftp = self.ssh.open_sftp()
        ftp.get(remotefile, localfile)
        ftp.close()

    def put_file(self, localfile, remotefile):
        if not self.is_connected():
            seqc.log.notify('You are not connected!')
            sys.exit(2)
        ftp = self.ssh.open_sftp()
        ftp.put(localfile, remotefile)
        seqc.log.info('Successfully placed {local_file} in {remote_file}.'.format(
            local_file=localfile, remote_file=remotefile))
        ftp.close()

    def exec_command(self, args):
        if not self.is_connected():
            seqc.log.notify('You are not connected!')
            sys.exit(2)
        stdin, stdout, stderr = self.ssh.exec_command(args)
        stdin.flush()
        data = stdout.read().decode().splitlines()  # response in bytes
        errs = stderr.read().decode().splitlines()
        return data, errs

